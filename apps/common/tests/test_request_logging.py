"""Tests for per-request tracing and latency logging.

The middleware is exercised directly with a ``RequestFactory`` request and a
stub ``get_response`` so the behaviour under test -- the header it echoes, the
id it exposes to the view, the line it logs, and the clean-up it does on the way
out -- is not entangled with any particular route.
"""

import logging
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.common.middleware import RequestContextMiddleware
from apps.common.request_context import (
    MAX_TRACE_ID_LENGTH,
    TRACE_ID_HEADER,
    get_request_id,
    reset_request_id,
    resolve_request_id,
    set_request_id,
)
from config.log_filters import RequestIdFilter
from config.logging_config import add_file_handler, file_log_handler


def make_log_record() -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, "message", None, None)


class ResolveRequestIdTests(SimpleTestCase):
    def test_a_supplied_id_is_used_verbatim(self):
        self.assertEqual(resolve_request_id("req-abc"), "req-abc")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(resolve_request_id("  req-abc  "), "req-abc")

    def test_a_blank_id_falls_back_to_a_generated_one(self):
        generated = resolve_request_id("")

        self.assertTrue(generated)
        self.assertNotEqual(generated, "")

    def test_a_missing_id_falls_back_to_a_generated_one(self):
        self.assertTrue(resolve_request_id(None))

    def test_an_over_long_id_is_clipped_to_the_audit_column(self):
        self.assertEqual(len(resolve_request_id("r" * 500)), MAX_TRACE_ID_LENGTH)

    def test_each_generated_id_is_distinct(self):
        self.assertNotEqual(resolve_request_id(None), resolve_request_id(None))


class RequestContextMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def build(self, *, status=200, callback=None):
        def get_response(request):
            if callback is not None:
                callback(request)
            return HttpResponse(status=status)

        return RequestContextMiddleware(get_response)

    def call(self, middleware, path="/api/v1/instruments/", **headers):
        request = self.factory.get(path, **headers)
        return middleware(request)

    def test_a_generated_trace_id_comes_back_on_the_response(self):
        response = self.call(self.build())

        self.assertTrue(response[TRACE_ID_HEADER])

    def test_a_caller_supplied_trace_id_is_echoed_unchanged(self):
        response = self.call(self.build(), HTTP_X_REQUEST_ID="req-9")

        self.assertEqual(response[TRACE_ID_HEADER], "req-9")

    def test_an_over_long_supplied_id_is_clipped_on_the_way_back(self):
        response = self.call(self.build(), HTTP_X_REQUEST_ID="r" * 500)

        self.assertEqual(len(response[TRACE_ID_HEADER]), MAX_TRACE_ID_LENGTH)

    def test_the_same_id_is_visible_to_the_view(self):
        seen = {}

        def callback(request):
            seen["trace_id"] = request.trace_id
            seen["request_id"] = request.request_id
            seen["ambient"] = get_request_id()

        response = self.call(self.build(callback=callback))

        self.assertEqual(seen["trace_id"], response[TRACE_ID_HEADER])
        self.assertEqual(seen["request_id"], response[TRACE_ID_HEADER])
        self.assertEqual(seen["ambient"], response[TRACE_ID_HEADER])

    def test_two_requests_get_two_ids(self):
        first = self.call(self.build())
        second = self.call(self.build())

        self.assertNotEqual(first[TRACE_ID_HEADER], second[TRACE_ID_HEADER])

    def test_the_ambient_id_is_cleared_after_the_response(self):
        self.call(self.build())

        self.assertEqual(get_request_id(), "")

    def test_the_ambient_id_is_cleared_even_when_the_view_raises(self):
        def get_response(request):
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            self.call(RequestContextMiddleware(get_response))

        self.assertEqual(get_request_id(), "")

    def test_it_logs_one_line_with_status_and_duration(self):
        with self.assertLogs("market_data.request", level="INFO") as captured:
            self.call(self.build(status=201))

        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.status_code, 201)
        self.assertEqual(record.http_method, "GET")
        self.assertIn("/api/v1/instruments/", record.path)
        self.assertGreaterEqual(record.duration_ms, 0)
        self.assertIn("-> 201 in", record.getMessage())

    def test_the_log_record_carries_the_trace_id(self):
        with self.assertLogs("market_data.request", level="INFO") as captured:
            response = self.call(self.build(), HTTP_X_REQUEST_ID="req-7")

        self.assertEqual(captured.records[0].request_id, "req-7")
        self.assertEqual(response[TRACE_ID_HEADER], "req-7")

    def test_an_anonymous_request_logs_no_actor(self):
        with self.assertLogs("market_data.request", level="INFO") as captured:
            self.call(self.build())

        self.assertEqual(captured.records[0].user_id, "-")

    def test_an_authenticated_request_logs_the_actor_id(self):
        def authenticate(request):
            request.user = SimpleNamespace(is_authenticated=True, pk=7)

        with self.assertLogs("market_data.request", level="INFO") as captured:
            self.call(self.build(callback=authenticate))

        self.assertEqual(captured.records[0].user_id, "7")

    @override_settings(API_SLOW_REQUEST_MS=1)
    def test_a_slow_request_is_logged_as_a_warning(self):
        # 50ms: comfortably longer than the ~15.6ms resolution of the monotonic
        # clock on Windows, so the measurement cannot round down to zero.
        def get_response(request):
            time.sleep(0.05)
            return HttpResponse(status=200)

        with self.assertLogs("market_data.request", level="WARNING") as captured:
            RequestContextMiddleware(get_response)(self.factory.get("/slow/"))

        self.assertEqual(captured.records[0].levelno, logging.WARNING)

    @override_settings(API_SLOW_REQUEST_MS=0)
    def test_escalation_can_be_switched_off(self):
        # A zero threshold disables the warning, so even a slow call stays INFO.
        with self.assertLogs("market_data.request", level="INFO") as captured:
            self.call(self.build())

        self.assertEqual(captured.records[0].levelno, logging.INFO)


class RequestIdFilterTests(SimpleTestCase):
    def test_outside_a_request_the_id_renders_as_a_dash(self):
        record = make_log_record()

        self.assertTrue(RequestIdFilter().filter(record))
        self.assertEqual(record.request_id, "-")

    def test_inside_a_request_it_carries_the_current_id(self):
        token = set_request_id("req-42")
        try:
            record = make_log_record()
            RequestIdFilter().filter(record)
        finally:
            reset_request_id(token)

        self.assertEqual(record.request_id, "req-42")


class TraceIdOnApiResponsesTests(SimpleTestCase):
    """The header is on real responses, not only on the middleware's own output.

    This drives the full request stack -- URLconf, DRF, the error handler, the
    middleware -- so it would catch the middleware being dropped from
    ``MIDDLEWARE`` as readily as a change in its behaviour. The 401 path is used
    because it needs no database and still exercises a complete API response.
    """

    def test_an_api_response_carries_a_generated_trace_id(self):
        response = self.client.get("/api/v1/instruments/")

        self.assertEqual(response.status_code, 401)
        self.assertTrue(response[TRACE_ID_HEADER])

    def test_an_api_response_echoes_the_callers_trace_id(self):
        response = self.client.get(
            "/api/v1/instruments/", headers={TRACE_ID_HEADER: "req-integration"}
        )

        self.assertEqual(response[TRACE_ID_HEADER], "req-integration")

    def test_the_trace_id_survives_an_error_response(self):
        response = self.client.get("/definitely-not-a-route/")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(response[TRACE_ID_HEADER])


class FileLogHandlerTests(SimpleTestCase):
    """The rotating plain-text file handler the API logs into."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, **overrides):
        kwargs = {
            "log_dir": self.tmp.name,
            "filename": "api.log",
            "max_bytes": 1024,
            "backup_count": 3,
        }
        kwargs.update(overrides)
        return file_log_handler(**kwargs)

    def test_it_is_a_rotating_utf8_text_file_sharing_the_console_formatter(self):
        handler = self.build()

        self.assertEqual(handler["class"], "logging.handlers.RotatingFileHandler")
        self.assertTrue(handler["filename"].endswith("api.log"))
        self.assertEqual(handler["maxBytes"], 1024)
        self.assertEqual(handler["backupCount"], 3)
        self.assertEqual(handler["encoding"], "utf-8")
        self.assertEqual(handler["formatter"], "verbose")
        self.assertEqual(
            handler["filters"], ["request_id", "suppress_probe_not_found"]
        )

    def test_it_creates_the_log_directory_when_missing(self):
        target = Path(self.tmp.name) / "nested" / "logs"

        file_log_handler(log_dir=target, filename="api.log")

        self.assertTrue(target.is_dir())

    def test_it_returns_none_instead_of_raising_when_the_path_is_unusable(self):
        # A file cannot become a directory, so the mkdir fails; the application
        # must still start, just without the file copy of the logs.
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("x", encoding="utf-8")

        self.assertIsNone(file_log_handler(log_dir=blocker / "logs", filename="api.log"))

    def test_add_file_handler_wires_the_root_and_every_named_logger(self):
        config = {
            "handlers": {},
            "root": {"handlers": ["console"]},
            "loggers": {
                "market_data": {"handlers": ["console"]},
                "market_data.request": {"handlers": ["console"]},
            },
        }

        add_file_handler(config, {"class": "x"})

        self.assertEqual(config["handlers"]["file"], {"class": "x"})
        self.assertEqual(config["root"]["handlers"], ["console", "file"])
        self.assertEqual(config["loggers"]["market_data"]["handlers"], ["console", "file"])
        self.assertEqual(
            config["loggers"]["market_data.request"]["handlers"], ["console", "file"]
        )


