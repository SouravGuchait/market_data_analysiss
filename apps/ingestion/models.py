"""Ingestion jobs, their source files, and the validation errors they produce."""

import uuid

from django.conf import settings
from django.db import models


class SourceType(models.TextChoices):
    CSV = "csv", "CSV"
    JSON = "json", "JSON"
    XLSX = "xlsx", "Excel workbook"
    API = "api", "API"
    MANUAL = "manual", "Manual"


class JobStatus(models.TextChoices):
    """The lifecycle of a job, as stored on the row.

    The database is the single source of business truth: Celery's own result is
    never consulted to answer "did this load succeed". A worker may die, Redis
    may be flushed, the result backend may be disabled entirely — the job row
    still says exactly where the load got to.

    ``PENDING``      created, not yet claimed by a worker (or queued for retry).
    ``PROCESSING``   a worker holds it and is loading rows. ``heartbeat_at``
                     advances while it runs, which is what makes a crashed
                     worker detectable rather than an indefinite hang.
    ``COMPLETED``    finished; every row was accepted, no calendar gap.
    ``COMPLETED_WITH_ERRORS``  finished; some rows were rejected or a trading
                     day was missing. The load is complete for what arrived.
    ``FAILED``       nothing usable was loaded, or the attempt was abandoned
                     after exhausting retries.
    ``CANCELLED``    a terminal state a client asked for; a running attempt
                     checks for it and stops at the next chunk boundary.
    """

    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors", "Completed with errors"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"

    @classmethod
    def terminal(cls) -> frozenset[str]:
        """States a job never leaves.

        Used to make repeated task execution a no-op: if a job is already
        terminal, a redelivered message must not load the file a second time.
        """
        return frozenset(
            {cls.COMPLETED, cls.COMPLETED_WITH_ERRORS, cls.FAILED, cls.CANCELLED}
        )

    @classmethod
    def active(cls) -> frozenset[str]:
        """States in which a load is in flight (or waiting to be)."""
        return frozenset({cls.PENDING, cls.PROCESSING})


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
        max_length=32, choices=JobStatus.choices, default=JobStatus.PENDING
    )

    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    rows_received = models.PositiveIntegerField(default=0)
    rows_accepted = models.PositiveIntegerField(default=0)
    rows_rejected = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True, default="")

    # --- worker bookkeeping ------------------------------------------------ #
    # The submission's deduplication key. Unique *per owner*: a client that
    # times out and retries with the same key gets the original job back rather
    # than a second load, while two different users uploading identical bytes
    # still get their own jobs. Blank on the plain create endpoint, which asks
    # for a job without a file and so has nothing to deduplicate on.
    idempotency_key = models.CharField(max_length=128, blank=True, null=True)

    # The Celery task currently (or last) responsible. Recorded so a stale job
    # can be tied back to the message that abandoned it.
    celery_task_id = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # How many times a worker has *started* this job. Bounded by the task's
    # max_retries; a count that keeps rising is the signal of a poison message.
    attempts = models.PositiveSmallIntegerField(default=0)

    # Progress, advanced as chunks are committed. ``rows_processed`` is
    # monotonic and survives a retry, so a resumed run reports honestly.
    rows_processed = models.PositiveIntegerField(default=0)

    # Written by the worker on a cadence while it runs. A PROCESSING job whose
    # heartbeat has gone cold is how a crashed worker is detected.
    heartbeat_at = models.DateTimeField(null=True, blank=True)

    # The last infrastructure failure, in one line. Distinct from
    # ``error_summary`` (which describes data quality): this is "why did the
    # attempt die", kept for the operator and never shown as a row finding.
    last_error = models.TextField(blank=True, default="")

    # The resume point for a chunked run: cumulative counters plus the ordered
    # row offset already committed. Empty for a job that has never run.
    checkpoint = models.JSONField(default=dict, blank=True)

    # The ingestion policy the submission asked for, frozen at submit time. The
    # worker reads it from here rather than from the request, so a queued job
    # does exactly what its submitter chose even if the API default later moves.
    policy = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["created_by", "idempotency_key"],
                name="uniq_job_owner_idempotency_key",
            ),
        ]
        indexes = [
            # Stale-job sweeps scan "PROCESSING with a cold heartbeat"; this is
            # the index that keeps that scan off a full table read.
            models.Index(fields=["status", "heartbeat_at"], name="idx_job_status_heartbeat"),
            # The job list is "this owner's jobs, newest first". The owner filter
            # alone was already served by the idempotency-key index, but the sort
            # was not: EXPLAIN over the benchmark data showed a "Sort row IDs:
            # requested_at DESC" node sitting on top of that lookup. Naming the
            # ordering here makes it part of the access path instead, so one page
            # is a bounded index read rather than a lookup plus a sort of
            # everything the owner has ever submitted.
            models.Index(
                fields=["created_by", "-requested_at"],
                name="idx_job_owner_requested",
            ),
        ]

    @property
    def is_terminal(self) -> bool:
        return self.status in JobStatus.terminal()

    @property
    def progress_percent(self) -> int:
        """Whole-number progress, or ``-1`` when the source has no known total.

        We deliberately do not guess a total for a streamed file: counting rows
        up front would mean a second pass over a file we opened once. When the
        checkpoint carries a ``total`` (an API page count, a worksheet's row
        count) the percentage is real; otherwise it is reported as unknown.
        """
        total = (self.checkpoint or {}).get("total")
        if not total:
            return -1
        return min(99, int(self.rows_processed * 100 / total))

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
    detail = models.CharField(max_length=255, blank=True, default="")
    raw_row = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [
            # "Show me every row that failed this rule" is the main access path.
            models.Index(fields=["error_code"], name="idx_validation_error_code"),
            models.Index(fields=["job", "row_number"], name="idx_validation_job_row"),
        ]

    def __str__(self):
        return f"{self.error_code} at row {self.row_number}"


class RuleStatus(models.TextChoices):
    PASS = "pass", "PASS"
    FAIL = "fail", "FAIL"


class IngestionRuleSummary(models.Model):
    """How one rule fared across one job: rows that passed it, rows that failed.

    This is the rule-level PASS/FAIL record. Per-row failure detail lives in
    ``IngestionValidationError``; a row with no failure for a rule is, by
    construction, a pass for that rule, so storing a row per (row, rule) would
    be redundant bulk. Counting is cheap and answers "did this rule hold?".
    """

    job = models.ForeignKey(
        IngestionJob, on_delete=models.CASCADE, related_name="rule_summaries"
    )
    rule_name = models.CharField(max_length=64)
    passed = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["rule_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "rule_name"], name="uniq_rule_summary_job_rule"
            ),
        ]

    @property
    def status(self):
        """FAIL if any row failed this rule, otherwise PASS."""
        return RuleStatus.PASS if self.failed == 0 else RuleStatus.FAIL

    def __str__(self):
        return f"{self.rule_name}: {self.passed} pass / {self.failed} fail"
