"""Writing audit events.

Recording fails *open*, deliberately: an audit row is a side record of an
operation that has already been decided, so a failure to write it must not turn a
successful upload into a ``500``. The trade-off is that a lost event is silent at
the call site, which is why failures are logged at ``ERROR`` with the action that
could not be recorded -- an operator can alert on that line, and the alternative
(failing the business operation because the audit table was unhappy) is worse.
"""

from __future__ import annotations

import logging
import uuid

from .events import sanitize_metadata
from .models import AuditEvent

logger = logging.getLogger(__name__)

#: A client may send ``X-Request-ID`` to tie an audit row to its own logs;
#: otherwise one is generated, so ``request_id`` -- which is indexed -- is never
#: blank and can always be queried.
REQUEST_ID_HEADER = "X-Request-ID"


def _request_id(request) -> str:
    if request is None:
        return uuid.uuid4().hex
    supplied = (request.headers.get(REQUEST_ID_HEADER) or "").strip()
    return (supplied or uuid.uuid4().hex)[:64]


def _client_ip(request):
    """The direct peer address, and *not* ``X-Forwarded-For``.

    A header is client-controlled unless a trusted proxy is configured to rewrite
    it, so recording it verbatim would let a caller choose the IP that lands in a
    compliance record -- worse than no IP at all. Behind a real proxy this is the
    one function to change, and the change belongs with the proxy configuration,
    not here.
    """
    if request is None:
        return None
    return request.META.get("REMOTE_ADDR") or None


def record_event(
    *,
    action,
    user=None,
    object_type="",
    object_id="",
    request=None,
    metadata=None,
):
    """Write one audit row. Returns it, or ``None`` if the write failed.

    Contract
    --------
    in : ``action`` -- an :class:`~apps.audit.events.AuditAction`; the actor as
         either ``user`` or a ``request`` (whose authenticated ``request.user`` is
         used when ``user`` is omitted); the target's ``object_type`` and
         ``object_id``; and optional ``metadata``.
    out: the created :class:`~apps.audit.models.AuditEvent`, or ``None`` when the
         insert failed and was swallowed.

    Business purpose: the single write path for the audit trail. Every caller goes
    through it, so the request-id, the peer address, and the metadata sanitizer
    are applied to every event rather than to the ones whose author remembered to
    apply them.
    """
    actor = user
    if actor is None and request is not None:
        candidate = getattr(request, "user", None)
        if candidate is not None and candidate.is_authenticated:
            actor = candidate

    try:
        return AuditEvent.objects.create(
            user=actor,
            action=action,
            object_type=object_type,
            object_id=str(object_id)[:64],
            request_id=_request_id(request),
            ip_address=_client_ip(request),
            metadata=sanitize_metadata(metadata) if metadata is not None else None,
        )
    except Exception:
        # The comment above is the contract; this line is where it is kept.
        logger.exception("could not record audit event %s", action)
        return None


def audit_event_queryset():
    """Every audit event, newest first, with its actor joined.

    ``select_related`` because every representation shows the actor's email;
    without it a page of events is one query plus one per distinct actor. The
    ordering comes from the model's ``Meta.ordering`` and is served by the
    ``idx_audit_created`` index.
    """
    return AuditEvent.objects.select_related("user")
