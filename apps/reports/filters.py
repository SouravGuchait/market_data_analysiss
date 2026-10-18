"""Query-parameter filters for the audit log.

Each parameter maps to a lookup the table can answer, so the log stays a fan-out
over indexed columns rather than a scan that materialises everything and filters
in Python.
"""

import django_filters as filters

from apps.audit.models import AuditEvent


class AuditEventFilter(filters.FilterSet):
    """Maps the audit log's query parameters onto ``AuditEvent`` lookups.

    ``user`` is the actor's email and ``object_id`` the target's id, because those
    are the two things a reader of the log already has in hand; neither is a
    primary key the reader would have to look up first. ``since``/``until`` bound
    ``created_at``, which is what ``idx_audit_created`` is ordered by.

    ``action`` and ``object_type`` are exact: they are small, known vocabularies,
    so a partial match would only ever widen a query that is already precise.
    """

    action = filters.CharFilter(field_name="action", lookup_expr="exact")
    object_type = filters.CharFilter(field_name="object_type", lookup_expr="exact")
    object_id = filters.CharFilter(field_name="object_id", lookup_expr="exact")
    user = filters.CharFilter(field_name="user__email", lookup_expr="iexact")
    request_id = filters.CharFilter(field_name="request_id", lookup_expr="exact")
    since = filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    until = filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = AuditEvent
        fields: list[str] = [
            "action",
            "object_type",
            "object_id",
            "user",
            "request_id",
            "since",
            "until",
        ]
