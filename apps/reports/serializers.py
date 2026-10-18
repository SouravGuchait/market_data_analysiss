"""Serializers for the reporting endpoints.

Most reports here are *computed documents* -- aggregates with no model behind
them -- so they need no serializer and are returned as plain dicts. The audit log
is the exception: it returns ``AuditEvent`` rows, so it gets a ``ModelSerializer``
like every other list in the project.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.audit.models import AuditEvent


class AuditEventSerializer(serializers.ModelSerializer):
    """One audit row, with the actor flattened to an email.

    ``user`` is a nullable foreign key (``SET_NULL``), so an event outlives the
    account it names -- and the row is still reported, with a ``null`` email,
    rather than being dropped by an inner join. The actor is flattened rather than
    nested for the same reason a job flattens its owner: the identity is all the
    row needs.
    """

    user_email = serializers.SerializerMethodField()

    class Meta:
        model = AuditEvent
        fields: list[str] = [
            "id",
            "created_at",
            "user_email",
            "action",
            "object_type",
            "object_id",
            "request_id",
            "ip_address",
            "metadata",
        ]
        # Audit rows are append-only; the API only ever reads them.
        read_only_fields: list[str] = fields

    def get_user_email(self, obj):
        return obj.user.email if obj.user_id else None
