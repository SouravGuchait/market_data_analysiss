"""Serializers for ingestion jobs and their validation errors."""

from rest_framework import serializers

from .models import IngestionJob, IngestionValidationError


class IngestionJobSerializer(serializers.ModelSerializer):
    """Output representation of a job.

    ``created_by_email`` is flattened rather than nested: the owner's identity is
    all a job needs, and nesting would pull in the whole user object.
    """

    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)

    class Meta:
        model = IngestionJob
        fields = [
            "id",
            "source_type",
            "status",
            "requested_at",
            "started_at",
            "completed_at",
            "rows_received",
            "rows_accepted",
            "rows_rejected",
            "error_summary",
            "created_by_email",
        ]
        # A job's lifecycle is driven by the pipeline, so only the two fields a
        # client legitimately supplies are writable and the rest are read-only.
        read_only_fields: list[str] = fields


class IngestionJobCreateSerializer(serializers.ModelSerializer):
    """Input validation for creating a job.

    Exposes only ``source_type``. Status, timestamps, and row counts are omitted
    rather than merely read-only, so they never appear in the request schema at
    all.
    """

    class Meta:
        model = IngestionJob
        fields = ["source_type"]


class IngestionValidationErrorSerializer(serializers.ModelSerializer):
    """Output representation of a rejected row."""

    class Meta:
        model = IngestionValidationError
        fields: list[str] = ["id", "row_number", "field_name", "error_code", "raw_row"]
