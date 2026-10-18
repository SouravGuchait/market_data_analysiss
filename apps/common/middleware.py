"""Per-request tracing and latency logging.

One middleware does three things for every request that passes through it:

1. **Assigns a trace id.** It echoes the caller's ``X-Request-ID`` header when
   one is present, otherwise it generates one. The id is stashed on the request,
   put in the ambient :mod:`~apps.common.request_context`, and written back on the
   response as the same header -- so a client, our logs, and the audit trail all
   name the same request.
2. **Times the request.** The clock starts before the view and stops after it,
   measuring the full request/response span rather than just the view body.
3. **Logs one line per request** -- method, path, status, duration, actor -- on
   the ``market_data.request`` logger. A request slower than
   ``settings.API_SLOW_REQUEST_MS`` is logged at ``WARNING`` so it stands out from
   the routine ``INFO`` line.

The timing wraps the view in a ``try`` so a request that raises is still logged
(with its status rendered as ``500``) and the ambient id is always reset -- a
leaked id would mislabel the next request on a reused worker.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings

from .request_context import (
    TRACE_ID_HEADER,
    get_request_id,
    reset_request_id,
    resolve_request_id,
    set_request_id,
)

logger = logging.getLogger("market_data.request")

#: When a view raises, no response object carries a status; we still want a
#: number in the log line, and the framework will render a 500.
SERVER_ERROR_STATUS = 500


def _user_id(request) -> str:
    """The authenticated actor's id, or ``"-"``.

    Read *after* the view so authentication has already run and the resolver is
    warm; the check is guarded so a non-DRF request that never sets ``user``
    cannot turn a log line into an error.
    """
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return "-"
    return str(getattr(user, "pk", "-") or "-")


class RequestContextMiddleware:
    """Attach a trace id, measure latency, and log one line per request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        supplied = request.headers.get(TRACE_ID_HEADER)
        trace_id = resolve_request_id(supplied)
        token = set_request_id(trace_id)
        # Both names are set: ``trace_id`` reads well in a view, ``request_id``
        # matches the audit column that the writer reads.
        request.trace_id = trace_id
        request.request_id = trace_id

        started = time.monotonic()
        try:
            response = self.get_response(request)
        except Exception:
            self._log(request, SERVER_ERROR_STATUS, started, trace_id)
            raise
        else:
            self._log(request, response.status_code, started, trace_id)
            response[TRACE_ID_HEADER] = trace_id
            return response
        finally:
            reset_request_id(token)

    def _log(self, request, status: int, started: float, trace_id: str) -> None:
        duration_ms = (time.monotonic() - started) * 1000
        slow_after = getattr(settings, "API_SLOW_REQUEST_MS", 0) or 0

        level = logging.WARNING if slow_after and duration_ms >= slow_after else logging.INFO
        user_id = _user_id(request)
        logger.log(
            level,
            "%s %s -> %s in %.2fms user=%s",
            request.method,
            request.get_full_path(),
            status,
            duration_ms,
            user_id,
            extra={
                "request_id": trace_id or get_request_id(),
                "http_method": request.method,
                "path": request.get_full_path(),
                "status_code": status,
                "duration_ms": round(duration_ms, 3),
                "user_id": user_id,
            },
        )
