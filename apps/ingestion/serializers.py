"""Serializers for ingestion jobs, their source files, and their findings."""

from rest_framework import serializers

from .etl import storage
from .etl.errors import UnsafeFileError
from .models import (
    IngestionFile,
    IngestionJob,
    IngestionRuleSummary,
    IngestionValidationError,
    SourceType,
)


class IngestionJobSerializer(serializers.ModelSerializer):
    """Output representation of a job.

    ``created_by_email`` is flattened rather than nested: the owner's identity is
    all a job needs, and nesting would pull in the whole user object.
    """

    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)

    class Meta:
        model = IngestionJob
        fields: list[str] = [
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
        fields: list[str] = ["source_type"]


class IngestionFileSerializer(serializers.ModelSerializer):
    """Output representation of a stored source file.

    ``storage_path`` is deliberately omitted: it is a server-side detail, and
    exposing it would leak the layout of the deployment for no client benefit.
    ``checksum`` is included because it is the file's identity, which a client
    can use to recognise a re-upload without fetching the file back.
    """

    class Meta:
        model = IngestionFile
        fields: list[str] = ["id", "original_filename", "content_type", "checksum"]


class IngestionValidationErrorSerializer(serializers.ModelSerializer):
    """Output representation of a rejected row."""

    class Meta:
        model = IngestionValidationError
        fields: list[str] = ["id", "row_number", "field_name", "error_code", "raw_row"]


class IngestionRuleSummarySerializer(serializers.ModelSerializer):
    """Output representation of one rule's PASS/FAIL verdict for a job.

    ``status`` is the model's derived property, so a client reads the verdict
    without re-deriving it from ``failed``.
    """

    status = serializers.CharField(read_only=True)

    class Meta:
        model = IngestionRuleSummary
        fields: list[str] = ["rule_name", "passed", "failed", "status"]


class IngestionUploadSerializer(serializers.Serializer):
    """Input validation for uploading a file and running the ETL.

    ``source_type`` is optional — it is inferred from the file's extension when
    omitted. ``on_invalid`` and ``on_existing`` are the two
    :class:`~apps.ingestion.etl.pipeline.IngestionPolicy` decisions, constrained
    here so an impossible policy is a ``400`` rather than a mid-load failure.
    """

    file = serializers.FileField()
    source_type = serializers.ChoiceField(
        choices=[SourceType.CSV, SourceType.JSON, SourceType.XLSX], required=False
    )
    sheet_name = serializers.CharField(required=False, allow_blank=False)
    on_invalid = serializers.ChoiceField(choices=["skip", "abort"], default="skip")
    on_existing = serializers.ChoiceField(
        choices=["update", "skip", "fail"], default="update"
    )

    def validate_file(self, value):
        """Apply the storage layer's safety checks as field errors.

        The service repeats these checks, but doing them here turns a rejected
        file into a clear ``400`` naming the ``file`` field instead of a job that
        exists only to fail.
        """
        try:
            name = storage.safe_filename(value.name)
            storage.validate_extension(name)
            storage.assert_within_size(value.size)
        except UnsafeFileError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value
