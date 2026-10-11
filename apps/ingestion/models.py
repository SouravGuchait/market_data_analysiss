"""Ingestion jobs, their source files, and the validation errors they produce."""

import uuid

from django.conf import settings
from django.db import models


class SourceType(models.TextChoices):
    CSV = "csv", "CSV"
    JSON = "json", "JSON"
    API = "api", "API"
    MANUAL = "manual", "Manual"


class JobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    PARTIAL = "partial", "Partial"


class IngestionJob(models.Model):
    """One load run, from request to completion, with its row counts.

    The header of a group of related rows: files ingested, prices written, and
    validation findings raised.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ingestion_jobs",
    )
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    status = models.CharField(
        max_length=16, choices=JobStatus.choices, default=JobStatus.PENDING
    )

    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    rows_received = models.PositiveIntegerField(default=0)
    rows_accepted = models.PositiveIntegerField(default=0)
    rows_rejected = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"{self.source_type} job {self.pk} ({self.status})"


class IngestionFile(models.Model):
    """The uploaded artifact a job ran on, with a checksum for idempotency."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    job = models.ForeignKey(
        IngestionJob, on_delete=models.CASCADE, related_name="files"
    )
    original_filename = models.CharField(max_length=255)
    storage_path = models.CharField(max_length=1024)
    checksum = models.CharField(max_length=64)
    content_type = models.CharField(max_length=128)

    class Meta:
        # A checksum is the identity of a file's content, so re-uploading the
        # same bytes for the same job is a duplicate, not a second file.
        constraints = [
            models.UniqueConstraint(
                fields=["job", "checksum"], name="uniq_file_job_checksum"
            ),
        ]

    def __str__(self):
        return self.original_filename


class IngestionValidationError(models.Model):
    """A single rejected row, kept so the job's counts reconcile.

    Named for the domain entity rather than ``ValidationError`` to avoid
    shadowing ``django.core.exceptions.ValidationError``, which is imported
    throughout Django and would be ambiguous.
    """

    job = models.ForeignKey(
        IngestionJob, on_delete=models.CASCADE, related_name="validation_errors"
    )
    row_number = models.PositiveIntegerField()
    field_name = models.CharField(max_length=128, blank=True, default="")
    error_code = models.CharField(max_length=64)
    raw_row = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [
            # "Show me every row that failed this rule" is the main access path.
            models.Index(fields=["error_code"], name="idx_validation_error_code"),
        ]

    def __str__(self):
        return f"{self.error_code} at row {self.row_number}"
