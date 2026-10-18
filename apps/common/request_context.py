"""The request id that ties a request to its logs, its audit rows, and its response.

A single ``ContextVar`` holds the current request's id. The middleware sets it at
the start of a request and resets it at the end, so any log line emitted while the
request is in flight -- from the view, a service, or the cache -- can carry the
same correlation id without every function having to pass one down.

A ``ContextVar`` rather than a thread local because it is the mechanism that stays
correct if a deployment ever runs async views: each task gets its own value rather
than sharing one across concurrent requests on the same worker.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

#: The id of the request being handled on this context, or ``""`` outside one.
_request_id: ContextVar[str] = ContextVar("request_id", default="")

#: A client may send this header to tie our logs to its own; the middleware
#: falls back to a generated id when it is absent, so a request is never logged
#: without a correlation id.
TRACE_ID_HEADER = "X-Request-ID"

#: The audit column is ``CharField(max_length=64)``; ids are clipped to it so a
#: caller-supplied header cannot overflow the column or bloat every log line.
MAX_TRACE_ID_LENGTH = 64


def new_request_id() -> str:
    """A fresh, opaque request id. Hex rather than dashed: 32 chars, URL-safe."""
    return uuid.uuid4().hex


def get_request_id() -> str:
    """The current request's id, or ``""`` when there is no request in context."""
    return _request_id.get()


def set_request_id(value: str):
    """Install ``value`` as the current id. Returns a token for :func:`reset_request_id`."""
    return _request_id.set(value)


def reset_request_id(token) -> None:
    """Restore the id that was current before the matching set.

    Called in the middleware's ``finally`` so a recycled worker thread cannot leak
    one request's id into the next request or into a background task.
    """
    _request_id.reset(token)


def resolve_request_id(supplied: str | None) -> str:
    """The id to use for a request: the caller's if it sent one, else a new one.

    Shared by the middleware and the audit writer so both agree on the value.
    """
    candidate = (supplied or "").strip()
    return (candidate or new_request_id())[:MAX_TRACE_ID_LENGTH]
