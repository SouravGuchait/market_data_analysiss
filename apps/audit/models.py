"""Audit events: who did what, to which object, and when."""

from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    """An immutable record of a privileged or state-changing action.

    Never updated or deleted by the application - the point is that it survives
    mistakes elsewhere in the system.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=128)
    object_type = models.CharField(max_length=128, blank=True, default="")
    # String rather than a FK: audit targets span every table, most of which use
    # integer PKs and two of which use UUIDs, so one generic column is the only
    # thing that fits them all.
    object_id = models.CharField(max_length=64, blank=True, default="")
    request_id = models.CharField(max_length=64, db_index=True)
    # GenericIPAddressField maps to varchar(39) on MySQL, which holds both IPv4
    # and IPv6. MySQL has no native inet type.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The administrator's log is the whole table, newest first, with no
            # owner filter -- and this is the one table in the project that grows
            # without bound. ``idx_audit_user_created`` cannot serve that read,
            # because a leading ``user`` column is exactly what an unfiltered
            # listing does not have; without this index the default page sorts
            # every row before returning any. Measured, not assumed: see
            # docs/reports.md (report 3), where the plan goes from
            # ``USE TEMP B-TREE FOR ORDER BY`` to an ordered index scan.
            models.Index(fields=["-created_at"], name="idx_audit_created"),
            # "Everything user X did, newest first" and "what happened to
            # object Y" are the two questions asked of this table.
            models.Index(
                fields=["user", "-created_at"], name="idx_audit_user_created"
            ),
            models.Index(
                fields=["object_type", "object_id"], name="idx_audit_object"
            ),
        ]

    def __str__(self):
        return f"{self.action} by {self.user_id} at {self.created_at}"
