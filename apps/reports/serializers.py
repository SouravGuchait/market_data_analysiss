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

    def get_user_email(self, obj) -> str | None:
        return obj.user.email if obj.user_id else None


# --------------------------------------------------------------------------- #
# Output shapes for the interactive API docs (/docs/)
#
# The two analytics reports are computed documents -- aggregates with no model
# behind them -- so they have no ``ModelSerializer``. They are named here and
# referenced from the views' ``@extend_schema``; the views still render the same
# dictionaries the services return.
# --------------------------------------------------------------------------- #


class ReportUserSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(read_only=True)


class UserReportTotalsSerializer(serializers.Serializer):
    jobs = serializers.IntegerField(read_only=True)
    successful = serializers.IntegerField(read_only=True)
    partially_successful = serializers.IntegerField(read_only=True)
    failed = serializers.IntegerField(read_only=True)


class PlatformReportTotalsSerializer(UserReportTotalsSerializer):
    users_with_jobs = serializers.IntegerField(read_only=True)
    active = serializers.IntegerField(read_only=True)


class RecordTotalsSerializer(serializers.Serializer):
    processed = serializers.IntegerField(read_only=True)
    accepted = serializers.IntegerField(read_only=True)
    rejected = serializers.IntegerField(read_only=True)


class DurationSummarySerializer(serializers.Serializer):
    measured_jobs = serializers.IntegerField(read_only=True)
    average_seconds = serializers.FloatField(read_only=True, allow_null=True)
    maximum_seconds = serializers.FloatField(read_only=True, allow_null=True)


class JobSummarySerializer(serializers.Serializer):
    """The compact "most recent job" shape a report quotes."""

    id = serializers.UUIDField(read_only=True)
    source_type = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    requested_at = serializers.DateTimeField(read_only=True)
    completed_at = serializers.DateTimeField(read_only=True, allow_null=True)
    rows_received = serializers.IntegerField(read_only=True)
    rows_accepted = serializers.IntegerField(read_only=True)
    rows_rejected = serializers.IntegerField(read_only=True)


class DailyJobCountSerializer(serializers.Serializer):
    date = serializers.DateField(read_only=True)
    count = serializers.IntegerField(read_only=True)


class UserAnalyticsSerializer(serializers.Serializer):
    """`GET /reports/me/`: the caller's own ingestion analytics."""

    user = ReportUserSerializer(read_only=True)
    totals = UserReportTotalsSerializer(read_only=True)
    records = RecordTotalsSerializer(read_only=True)
    durations = DurationSummarySerializer(read_only=True)
    most_recent_job = JobSummarySerializer(read_only=True, allow_null=True)
    daily_job_counts = DailyJobCountSerializer(many=True, read_only=True)
    by_status = serializers.DictField(child=serializers.IntegerField(), read_only=True)
    by_source = serializers.DictField(child=serializers.IntegerField(), read_only=True)


class TopUserSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(read_only=True)
    jobs = serializers.IntegerField(read_only=True)
    successful = serializers.IntegerField(read_only=True)
    failed = serializers.IntegerField(read_only=True)


class FailureJobSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    created_by_email = serializers.EmailField(read_only=True)
    source_type = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    requested_at = serializers.DateTimeField(read_only=True)
    completed_at = serializers.DateTimeField(read_only=True, allow_null=True)
    rows_received = serializers.IntegerField(read_only=True)
    rows_accepted = serializers.IntegerField(read_only=True)
    rows_rejected = serializers.IntegerField(read_only=True)
    last_error = serializers.CharField(read_only=True, allow_blank=True, allow_null=True)
    error_summary = serializers.CharField(read_only=True, allow_blank=True)


class ErrorCodeCountSerializer(serializers.Serializer):
    error_code = serializers.CharField(read_only=True)
    count = serializers.IntegerField(read_only=True)


class FailureSummarySerializer(serializers.Serializer):
    failed_jobs = serializers.IntegerField(read_only=True)
    recent = FailureJobSerializer(many=True, read_only=True)
    top_error_codes = ErrorCodeCountSerializer(many=True, read_only=True)


class PlatformAnalyticsSerializer(serializers.Serializer):
    """`GET /reports/platform/`: platform-wide ingestion analytics."""

    totals = PlatformReportTotalsSerializer(read_only=True)
    records = RecordTotalsSerializer(read_only=True)
    durations = DurationSummarySerializer(read_only=True)
    by_status = serializers.DictField(child=serializers.IntegerField(), read_only=True)
    by_source = serializers.DictField(child=serializers.IntegerField(), read_only=True)
    daily_job_counts = DailyJobCountSerializer(many=True, read_only=True)
    top_users = TopUserSerializer(many=True, read_only=True)
    failures = FailureSummarySerializer(read_only=True)
