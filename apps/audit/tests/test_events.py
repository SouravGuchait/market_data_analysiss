"""Unit tests for the audit vocabulary: the sanitizer and the recorder.

``sanitize_metadata`` must be the thing that keeps credentials out, and
``record_event`` must fail *open* rather than take a business operation down with
it. The operations that record are covered in ``test_operations.py``.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase

from apps.audit import services
from apps.audit.events import (
    MAX_STRING_LENGTH,
    NEVER_LOGGED_KEYS,
    AuditAction,
    sanitize_metadata,
)
from apps.audit.models import AuditEvent

PASSWORD = "drf-portfolio-42!"


class SanitizeMetadataTests(TestCase):
    def test_forbidden_keys_are_dropped(self):
        cleaned = sanitize_metadata(
            {"source_type": "csv", "password": "hunter2", "access_token": "jwt"}
        )

        self.assertEqual(cleaned, {"source_type": "csv"})
        self.assertNotIn("password", cleaned)
        self.assertNotIn("access_token", cleaned)

    def test_the_deny_list_is_case_insensitive(self):
        cleaned = sanitize_metadata({"Password": "x", "TOKEN": "y", "keep": 1})

        self.assertEqual(cleaned, {"keep": 1})

    def test_forbidden_keys_are_dropped_at_every_level(self):
        cleaned = sanitize_metadata(
            {"outer": {"secret": "s", "nested": {"sessionid": "abc", "ok": True}}}
        )

        self.assertEqual(cleaned, {"outer": {"nested": {"ok": True}}})

    def test_every_deny_listed_key_is_actually_filtered(self):
        # One test over the whole vocabulary, so a key added to NEVER_LOGGED_KEYS
        # without a matching check cannot slip through.
        cleaned = sanitize_metadata({key: "value" for key in NEVER_LOGGED_KEYS})

        self.assertEqual(cleaned, {})

    def test_long_strings_are_truncated(self):
        cleaned = sanitize_metadata({"body": "x" * (MAX_STRING_LENGTH + 100)})

        self.assertEqual(len(cleaned["body"]), MAX_STRING_LENGTH)

    def test_non_json_values_are_coerced_rather_than_raising(self):
        from decimal import Decimal

        cleaned = sanitize_metadata({"amount": Decimal("1.5"), "thing": object()})

        self.assertEqual(cleaned["amount"], "1.5")
        self.assertIsInstance(cleaned["thing"], str)

    def test_primitives_lists_and_none_survive_intact(self):
        cleaned = sanitize_metadata(
            {"i": 1, "f": 2.5, "b": True, "n": None, "items": [1, "two", {"password": "x"}]}
        )

        self.assertEqual(
            cleaned,
            {"i": 1, "f": 2.5, "b": True, "n": None, "items": [1, "two", {}]},
        )
        self.assertIs(cleaned["b"], True)

    def test_a_pathological_payload_is_bounded_not_fatal(self):
        deep = current = {}
        for _ in range(50):
            current["next"] = {}
            current = current["next"]

        cleaned = sanitize_metadata(deep)

        self.assertIn("<truncated>", str(cleaned))


class RecordEventTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="analyst@example.com", password=PASSWORD
        )
        self.factory = RequestFactory()

    def request(self, *, user=None, request_id=None, ip="203.0.113.7"):
        extra = {"REMOTE_ADDR": ip}
        if request_id:
            extra["HTTP_X_REQUEST_ID"] = request_id
        request = self.factory.post("/api/v1/ingestion/", **extra)
        request.user = user if user is not None else self.user
        return request

    def test_an_explicit_user_is_recorded_as_the_actor(self):
        event = services.record_event(action=AuditAction.JOB_CREATED, user=self.user)

        self.assertEqual(event.user, self.user)
        self.assertEqual(event.action, "ingestion.job_created")

    def test_the_actor_request_id_and_ip_come_from_the_request(self):
        event = services.record_event(
            action=AuditAction.JOB_CREATED, request=self.request(request_id="req-9")
        )

        self.assertEqual(event.user, self.user)
        self.assertEqual(event.request_id, "req-9")
        self.assertEqual(event.ip_address, "203.0.113.7")

    def test_the_middleware_assigned_request_id_wins_over_the_header(self):
        # The middleware resolves one id per request and puts it on the request,
        # so the audit row, the response header, and the request log agree even
        # when a header was also present.
        request = self.request(request_id="from-header")
        request.request_id = "from-middleware"

        event = services.record_event(
            action=AuditAction.JOB_CREATED, request=request
        )

        self.assertEqual(event.request_id, "from-middleware")

    def test_a_missing_request_id_is_generated_not_left_blank(self):
        event = services.record_event(action=AuditAction.JOB_CREATED, request=self.request())

        self.assertTrue(event.request_id)

    def test_a_caller_supplied_request_id_is_truncated_to_the_column(self):
        event = services.record_event(
            action=AuditAction.JOB_CREATED, request=self.request(request_id="r" * 200)
        )

        self.assertEqual(len(event.request_id), 64)

    def test_an_anonymous_request_records_a_null_actor(self):
        event = services.record_event(
            action="auth.denied", request=self.request(user=AnonymousUser())
        )

        self.assertIsNone(event.user)
        self.assertIsNone(event.user_id)

    def test_metadata_is_sanitized_on_the_way_in(self):
        event = services.record_event(
            action=AuditAction.JOB_CREATED,
            user=self.user,
            metadata={"source_type": "csv", "password": "hunter2"},
        )

        self.assertEqual(event.metadata, {"source_type": "csv"})

    def test_no_metadata_stays_null(self):
        event = services.record_event(action=AuditAction.JOB_CREATED, user=self.user)

        self.assertIsNone(event.metadata)

    def test_a_failed_insert_is_swallowed_and_logged(self):
        # The contract: an audit write never breaks the operation it describes.
        with mock.patch.object(
            AuditEvent.objects, "create", side_effect=RuntimeError("audit table down")
        ), self.assertLogs("apps.audit.services", level="ERROR"):
            result = services.record_event(
                action=AuditAction.JOB_CREATED, user=self.user
            )

        self.assertIsNone(result)
        self.assertEqual(AuditEvent.objects.count(), 0)
