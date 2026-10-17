"""Business logic for ingestion jobs.

Two related jobs live here, kept in one module on purpose:

* the **read/create surface** the API talks to — which fields a client may set,
  what a job's initial state is, and who may see a job; and
* the **ETL entry points** — the synchronous engine in ``apps.ingestion.etl`` is
  driven from here, so the API, the management command, and the tests all call
  the same functions rather than reaching into ``etl`` directly.

A view — or, later, a Celery task — therefore never wires the pipeline itself:
it calls a service, and the service owns the wiring.
"""

from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.utils import timezone

from .etl import storage
from .etl.pipeline import IngestionPolicy, IngestionResult, run_ingestion
from .etl.reporting import job_report
from .models import IngestionJob, JobStatus, SourceType

VALID_SOURCE_TYPES = {choice.value for choice in SourceType}

# The one place a filename is mapped to a source type. ``storage`` allows the
# same extensions, so a mismatch between what can be stored and what can be read
# has a single cause rather than two independent lists.
_EXTENSION_TO_SOURCE_TYPE = {
    ".csv": SourceType.CSV,
    ".json": SourceType.JSON,
    ".jsonl": SourceType.JSON,
    ".xlsx": SourceType.XLSX,
}


# --------------------------------------------------------------------------- #
# Read / create surface (used by the API)
# --------------------------------------------------------------------------- #


def get_job_queryset():
    """The base queryset for every job read.

    ``select_related`` joins the owning user in one statement, because every job
    representation shows who created it.
    """
    return IngestionJob.objects.select_related("created_by")


def list_jobs_for_user(user, *, status=None, source_type=None):
    """Returns a user's own jobs, newest first."""
    queryset = get_job_queryset().filter(created_by=user)

    if status:
        queryset = queryset.filter(status=status)
    if source_type:
        queryset = queryset.filter(source_type=source_type)

    return queryset.order_by("-requested_at")


def get_job_for_user(user, pk):
    """Returns one job owned by ``user``, or raises IngestionJob.DoesNotExist.

    Scoping by owner inside the lookup is what makes cross-user access
    impossible: a job belonging to someone else is indistinguishable from a job
    that does not exist.
    """
    return get_job_queryset().get(pk=pk, created_by=user)


def create_job(*, created_by, source_type):
    """Creates a pending ingestion job owned by ``created_by``.

    Only the owner and the source type may be set. Status, timestamps, and row
    counts are all owned by the pipeline: a client sending ``status: completed``
    is rejected rather than trusted.
    """
    if source_type not in VALID_SOURCE_TYPES:
        raise ValidationError(
            {"source_type": [f"Must be one of: {', '.join(sorted(VALID_SOURCE_TYPES))}."]}
        )

    return IngestionJob.objects.create(created_by=created_by, source_type=source_type)


def get_validation_errors_for_job(job, *, error_code=None):
    """Returns the validation errors recorded against a job."""
    queryset = job.validation_errors.all()

    if error_code:
        queryset = queryset.filter(error_code=error_code)

    return queryset.order_by("row_number")


def get_rule_summaries_for_job(job):
    """Returns the rule-level PASS/FAIL verdicts recorded for a job.

    One row per rule the pipeline evaluated, in the model's default order (by
    rule name). Returned as a queryset so a view can serialize or paginate it.
    """
    return job.rule_summaries.all()


def get_job_statistics(user):
    """Aggregates a user's ingestion activity.

    Aggregated in SQL so the work happens in the database rather than by
    iterating jobs in Python.
    """
    from django.db.models import Count, Sum

    jobs = list_jobs_for_user(user)
    return jobs.aggregate(
        total_jobs=Count("id"),
        total_rows_received=Sum("rows_received"),
        total_rows_accepted=Sum("rows_accepted"),
        total_rows_rejected=Sum("rows_rejected"),
    )


# --------------------------------------------------------------------------- #
# ETL entry points (the synchronous engine)
# --------------------------------------------------------------------------- #


def infer_source_type(filename) -> str:
    """Map a filename to its :class:`~apps.ingestion.models.SourceType`.

    Contract
    --------
    in : any filename or path.
    out: the matching ``SourceType`` member.

    Business purpose: a caller that has a file but no declared source type — the
    management command, an upload without an explicit type — gets the right
    adapter chosen the same way everywhere.

    Raises ``ValueError`` for an extension the engine cannot parse, so the caller
    learns "not one of ours" up front rather than from a parse failure mid-load.
    """
    suffix = Path(str(filename)).suffix.lower()
    try:
        return _EXTENSION_TO_SOURCE_TYPE[suffix]
    except KeyError:
        allowed = ", ".join(sorted(_EXTENSION_TO_SOURCE_TYPE))
        raise ValueError(
            f"cannot infer a source type from {filename!r}; expected one of: {allowed}"
        ) from None


def run_job(
    job,
    *,
    source_type,
    path=None,
    fetcher=None,
    sheet_name=None,
    records_key=None,
    policy: IngestionPolicy | None = None,
) -> IngestionResult:
    """Run the synchronous ETL for an already-created job.

    Contract
    --------
    in : a ``pending`` job, its ``source_type``, and its source — a ``path`` for a
         file format, or a ``fetcher`` callable for the API source — plus an
         optional :class:`~apps.ingestion.etl.pipeline.IngestionPolicy`.
    out: an :class:`~apps.ingestion.etl.pipeline.IngestionResult`; the job row is
         updated in place and ends ``completed``, ``completed_with_errors``, or
         ``failed``.

    Business purpose: the single call that turns a job request into loaded rows,
    so every trigger — API, command, test — shares the same validation,
    reporting, and transaction behaviour.
    """
    return run_ingestion(
        job,
        source_type=source_type,
        path=path,
        fetcher=fetcher,
        sheet_name=sheet_name,
        records_key=records_key,
        policy=policy,
    )


def ingest_file(
    *,
    user,
    path,
    source_type: str | None = None,
    policy: IngestionPolicy | None = None,
    sheet_name: str | None = None,
) -> IngestionResult:
    """Create a job for a file on disk, register it, and run the ETL.

    Contract
    --------
    in : the owning user, a path, and optional overrides.

    out: the :class:`~apps.ingestion.etl.pipeline.IngestionResult`.

    Business purpose: the path the management command and the tests use. The file
    is validated (extension, size, existence) *before* the job is created, so a
    file that cannot be read leaves no half-made job behind.

    Raises ``ValueError`` for an unknown extension and
    :class:`~apps.ingestion.etl.errors.UnsafeFileError` for a missing or oversize
    file.
    """
    resolved_type = source_type or infer_source_type(path)
    resolved_path = storage.validate_local_file(path)
    job = create_job(created_by=user, source_type=resolved_type)
    storage.register_local_file(job=job, path=resolved_path)
    return run_job(
        job,
        source_type=resolved_type,
        path=resolved_path,
        sheet_name=sheet_name,
        policy=policy,
    )


def ingest_upload(
    *,
    user,
    uploaded_file,
    source_type: str | None = None,
    policy: IngestionPolicy | None = None,
    sheet_name: str | None = None,
) -> IngestionResult:
    """Create a job for an uploaded file, store it, and run the ETL.

    Contract
    --------
    in : the owning user and a Django ``UploadedFile``.

    out: the :class:`~apps.ingestion.etl.pipeline.IngestionResult`.

    Business purpose: the HTTP upload path. The file passes its safety checks
    before the job exists, is stored under ``MEDIA_ROOT`` with a checksum, and is
    then read back from storage — so the pipeline parses exactly the artifact the
    job is recorded against, not a buffer that might differ from what was saved.
    """
    name = storage.safe_filename(uploaded_file.name)
    storage.validate_extension(name)
    storage.assert_within_size(uploaded_file.size)

    resolved_type = source_type or infer_source_type(name)
    job = create_job(created_by=user, source_type=resolved_type)
    stored = storage.store_upload(job=job, uploaded_file=uploaded_file)
    return run_job(
        job,
        source_type=resolved_type,
        path=default_storage.path(stored.storage_path),
        sheet_name=sheet_name,
        policy=policy,
    )


def report_for(job) -> dict:
    """Return the data-quality report for a finished job as a plain dict.

    Contract
    --------
    in : an :class:`~apps.ingestion.models.IngestionJob`.

    out: the report produced by
         :func:`apps.ingestion.etl.reporting.job_report` — the row counts, one
         PASS/FAIL entry per rule, and a sample of the failures.

    Business purpose: the one shape a dashboard, a view, or a log line reads, so
    the rule table and the failure sample cannot drift apart.
    """
    return job_report(job)


# --------------------------------------------------------------------------- #
# Asynchronous entry points (the Celery path)
# --------------------------------------------------------------------------- #
#
# The synchronous functions above run the load inside the caller's request. The
# functions below hand the same load to a worker: they create the job row, hand
# the worker its primary key, and return. Everything that decides *what* the
# load does still lives in ``etl`` — the queue changes when the work happens,
# not what it means.


def policy_payload(policy: IngestionPolicy | None, *, sheet_name: str | None = None) -> dict:
    """Flatten a policy into the JSON frozen on the job row.

    The submitter's choices travel with the job rather than with the worker.
    That is what makes the queue safe: a job submitted on Monday does on
    Thursday exactly what it asked for on Monday, even if the API's defaults
    have moved in between.
    """
    resolved = policy or IngestionPolicy()
    payload = {
        "on_invalid": resolved.on_invalid,
        "on_existing": resolved.on_existing,
        "chunk_size": resolved.chunk_size,
        "max_errors": resolved.max_errors,
        "run_calendar_check": resolved.run_calendar_check,
    }
    if sheet_name:
        payload["sheet_name"] = sheet_name
    return payload


def create_or_get_job(*, created_by, source_type, idempotency_key, policy=None):
    """Create a job, or return the one this submission already created.

    Returns ``(job, created)``. The dedupe is enforced by a database constraint,
    not by a prior ``SELECT``: two concurrent submissions of the same file both
    attempt the insert, one wins, and the loser is turned into a lookup by the
    ``IntegrityError``. A read-then-write check would leave a window in which
    both requests see "no existing job" and both create one.
    """
    try:
        with transaction.atomic():
            job = IngestionJob.objects.create(
                created_by=created_by,
                source_type=source_type,
                idempotency_key=idempotency_key,
                policy=policy if isinstance(policy, dict) else policy_payload(policy),
            )
        return job, True
    except IntegrityError:
        existing = IngestionJob.objects.filter(
            created_by=created_by, idempotency_key=idempotency_key
        ).first()
        if existing is None:  # a different constraint fired; not ours to swallow
            raise
        return existing, False


def enqueue_ingestion_job(job) -> str:
    """Hand a committed job to the worker and record the task id.

    Contract
    --------
    in : an :class:`~apps.ingestion.models.IngestionJob` that has already been
         committed, with its source file stored.
    out: the Celery task id (empty if the broker returned none).

    Business purpose: the single place a job becomes work. It is called *after*
    the job's transaction commits — a worker that started before the row was
    visible would claim nothing, and worse, could be told to load a file whose
    row did not exist yet.
    """
    # Imported here rather than at module scope so that importing the service
    # layer never requires a live Celery application.
    from .tasks import process_ingestion_job

    async_result = process_ingestion_job.delay(str(job.pk))
    task_id = getattr(async_result, "id", "") or ""
    if task_id:
        IngestionJob.objects.filter(pk=job.pk).update(celery_task_id=task_id)
    return task_id


def submit_upload(
    *,
    user,
    uploaded_file,
    source_type: str | None = None,
    policy: IngestionPolicy | None = None,
    sheet_name: str | None = None,
    idempotency_key: str | None = None,
):
    """Store an upload, create-or-reuse its job, and queue it. Returns (job, created).

    Contract
    --------
    in : the owning user, an ``UploadedFile``, and an optional client-supplied
         ``Idempotency-Key``.
    out: ``(job, created)`` — the job to report on, and whether this call is the
         one that created it.

    Business purpose: the asynchronous equivalent of
    :func:`ingest_upload`. Two properties matter here:

    * the file is hashed *before* the job exists, so "the same file uploaded
      twice" collapses onto one job rather than relying on the browser to be
      well-behaved; and
    * the task is enqueued only after the file and its row are committed, so a
      worker can never be told to load something that is not there yet.
    """
    name = storage.safe_filename(uploaded_file.name)
    storage.validate_extension(name)
    storage.assert_within_size(uploaded_file.size)

    resolved_type = source_type or infer_source_type(name)

    # The digest is the default identity of a submission. A client that supplies
    # its own key gets to name the operation (so a retry after a timeout maps to
    # the same job even if the bytes were re-encoded in transit).
    digest = storage.checksum_of_upload(uploaded_file)
    key = (idempotency_key or f"file:{digest}")[:128]

    job, created = create_or_get_job(
        created_by=user,
        source_type=resolved_type,
        idempotency_key=key,
        policy=policy_payload(policy, sheet_name=sheet_name),
    )
    if not created:
        # A replay. Return the original job, still queued or already finished —
        # never a second load.
        return job, False

    try:
        storage.store_upload(job=job, uploaded_file=uploaded_file)
    except Exception as exc:  # storage failed after the row existed
        IngestionJob.objects.filter(pk=job.pk).update(
            status=JobStatus.FAILED,
            last_error=f"{type(exc).__name__}: {exc}"[:2000],
        )
        raise

    enqueue_ingestion_job(job)
    return job, True


def cancel_job(job) -> IngestionJob:
    """Ask a job to stop, or cancel one that has not started.

    A ``pending`` job is simply marked terminal — it will never be claimed,
    because the claim only matches ``pending``. A ``processing`` job is marked
    terminal too, and the running attempt notices at its next chunk boundary and
    finishes as ``cancelled``. Cancellation is therefore cooperative: it stops
    the load quickly, but between chunk boundaries, not mid-write.
    """
    if job.is_terminal:
        return job
    job.status = JobStatus.CANCELLED
    job.completed_at = timezone.now()
    job.save(update_fields=["status", "completed_at"])
    return job
