"""The worker task: claiming, idempotency, retries, resumption, cancellation.

Each test here answers one operational question, and the questions are the
failure modes that actually happen: a worker killed mid-load, a message
delivered twice, a database that goes away, a vendor API that times out, and a
job that simply takes a long time.

The theme throughout is that **at-least-once delivery is the assumption**. A
broker does not give exactly-once, so everything the worker does has to be safe
when it happens twice.
"""

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from celery.exceptions import Retry
from django.core.management import call_command
from django.db import IntegrityError, OperationalError
from django.utils import timezone

from apps.ingestion import monitoring, tasks
from apps.ingestion.etl import storage
from apps.ingestion.etl.failures import is_transient
from apps.ingestion.etl.pipeline import IngestionPolicy, run_ingestion
from apps.ingestion.models import IngestionJob, JobStatus, SourceType
from apps.marketdata.models import PriceRecord

from .helpers import make_instrument, make_user, write_csv

pytestmark = pytest.mark.django_db

# Six consecutive trading days, so a chunked run has something to split.
TRADING_DAYS = [
    "2024-03-11",
    "2024-03-12",
    "2024-03-13",
    "2024-03-14",
    "2024-03-15",
    "2024-03-18",
]


def rows_for(dates, symbol="AAPL", source="async"):
    return [
        [symbol, day, "100.00", "102.00", "99.00", "101.00", "101.00", 1000, source]
        for day in dates
    ]


def pending_job_with_file(tmp_path, user, rows=None, *, name="prices.csv"):
    """A ``pending`` job with a CSV already registered against it.

    Returns ``(job, path)``: the pipeline is driven directly in several tests and
    it wants the path, not the job's file row.
    """
    path = write_csv(tmp_path / name, rows if rows is not None else rows_for(TRADING_DAYS))
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)
    storage.register_local_file(job=job, path=path)
    return job, path


def run_task(job):
    """Run the task eagerly, in-process, and return what it returned.

    ``.apply()`` executes the real task body with a populated ``request``, so
    ``self.request.id`` and ``self.request.retries`` behave as they would in a
    worker — without a broker or a worker process. This is the "mock the task,
    not the pipeline" seam: everything below the task is the real thing.
    """
    return tasks.process_ingestion_job.apply(args=[str(job.pk)]).get()


class _FakeRequest:
    def __init__(self, retries=0):
        self.retries = retries


class _RecordingTask:
    """A stand-in for the bound Celery task that records the retry decision.

    ``_handle_exception`` is tested through this rather than through ``.retry()``
    because the celery retry machinery is itself under test here — using it to
    verify itself would prove nothing about the countdown or the attempt count.
    """

    def __init__(self, retries=0):
        self.request = _FakeRequest(retries)
        self.retry_kwargs = None

    def retry(self, **kwargs):
        self.retry_kwargs = kwargs
        raise Retry("retry requested")


# --------------------------------------------------------------------------- #
# Delivery guarantees: the happy path, and doing it twice
# --------------------------------------------------------------------------- #


def test_a_worker_loads_a_pending_job_and_completes_it(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    job, _ = pending_job_with_file(tmp_path, user)

    outcome = run_task(job)

    job.refresh_from_db()
    assert outcome == f"{JobStatus.COMPLETED}:{job.pk}"
    assert job.status == JobStatus.COMPLETED
    assert job.attempts == 1
    assert job.celery_task_id  # recorded so the message can be traced back
    assert (job.rows_received, job.rows_accepted, job.rows_rejected) == (6, 6, 0)
    assert job.rows_processed == 6
    assert PriceRecord.objects.count() == 6


def test_a_redelivered_message_for_a_finished_job_does_nothing(tmp_path):
    """The same message delivered twice must not load the file twice.

    The proof that no work happened is that the source file is *deleted* between
    the two deliveries: a task that tried to load again would have to fail.
    """
    make_instrument("AAPL")
    user = make_user()
    job, _ = pending_job_with_file(tmp_path, user)
    run_task(job)

    Path(job.files.first().storage_path).unlink()

    outcome = run_task(job)

    job.refresh_from_db()
    assert outcome == f"skipped:{JobStatus.COMPLETED}"
    assert job.attempts == 1, "the second delivery must not even claim the job"
    assert PriceRecord.objects.count() == 6


def test_only_one_delivery_can_claim_a_pending_job():
    """Two workers racing: the conditional UPDATE lets exactly one through."""
    user = make_user()
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)

    assert tasks.claim_job(str(job.pk), task_id="first") is True
    assert tasks.claim_job(str(job.pk), task_id="second") is False

    job.refresh_from_db()
    assert job.status == JobStatus.PROCESSING
    assert job.attempts == 1
    assert job.celery_task_id == "first"


def test_a_job_another_worker_is_processing_is_left_alone(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    job, _ = pending_job_with_file(tmp_path, user)
    IngestionJob.objects.filter(pk=job.pk).update(status=JobStatus.PROCESSING)

    assert run_task(job) == "skipped:claimed"

    job.refresh_from_db()
    assert job.status == JobStatus.PROCESSING  # still owned by the other worker
    assert PriceRecord.objects.count() == 0


def test_a_cancelled_job_is_never_claimed():
    """Cancelling a queued job is enough: the claim only matches ``pending``."""
    user = make_user()
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)
    IngestionJob.objects.filter(pk=job.pk).update(status=JobStatus.CANCELLED)

    assert run_task(job) == f"skipped:{JobStatus.CANCELLED}"


def test_a_task_for_an_unknown_job_is_ignored():
    job_id = str(uuid.uuid4())

    assert tasks.process_ingestion_job.apply(args=[job_id]).get() == f"missing:{job_id}"


def test_a_job_with_no_source_file_is_failed_without_reading_anything():
    user = make_user()
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)

    assert run_task(job) == f"failed:{job.pk}"

    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert "no source file" in job.last_error


def test_an_api_source_is_refused_by_the_worker_with_a_reason():
    """A callable cannot be serialised into a message, so say so rather than hang."""
    user = make_user()
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.API)

    outcome = run_task(job)

    job.refresh_from_db()
    assert outcome == f"failed:{job.pk}"
    assert job.status == JobStatus.FAILED
    assert "cannot be serialised" in job.last_error


# --------------------------------------------------------------------------- #
# Retry policy: transient retries, permanent does not
# --------------------------------------------------------------------------- #


def test_a_transient_failure_hands_the_job_back_and_asks_for_a_retry():
    """The job returns to ``pending`` so the retry can claim it again."""
    user = make_user()
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)
    task = _RecordingTask(retries=0)

    with pytest.raises(Retry):
        tasks._handle_exception(task, job, OperationalError("server has gone away"))

    job.refresh_from_db()
    assert job.status == JobStatus.PENDING
    assert "OperationalError" in job.last_error
    assert task.retry_kwargs["max_retries"] == 5
    assert 0 <= task.retry_kwargs["countdown"] <= 10  # first attempt: base delay


def test_a_transient_failure_that_has_run_out_of_attempts_is_failed(settings):
    settings.INGESTION_MAX_RETRIES = 3
    user = make_user()
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)
    task = _RecordingTask(retries=3)  # already used every attempt

    outcome = tasks._handle_exception(task, job, OperationalError("still gone"))

    job.refresh_from_db()
    assert outcome == f"failed:{job.pk}"
    assert job.status == JobStatus.FAILED
    assert task.retry_kwargs is None, "a job out of attempts must not be retried"
    assert "OperationalError" in job.last_error


def test_a_permanent_failure_is_not_retried():
    """A duplicate key will be a duplicate key again; retrying only wastes time."""
    user = make_user()
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)
    task = _RecordingTask(retries=0)

    outcome = tasks._handle_exception(task, job, IntegrityError("duplicate key"))

    job.refresh_from_db()
    assert outcome == f"failed:{job.pk}"
    assert job.status == JobStatus.FAILED
    assert task.retry_kwargs is None


def test_the_worker_routes_a_failure_from_the_load_to_the_classifier(tmp_path, monkeypatch):
    """The task's catch-all must not swallow: it must hand the error to the policy."""
    make_instrument("AAPL")
    user = make_user()
    job, _ = pending_job_with_file(tmp_path, user)

    boom = OperationalError("gone away")

    def explode(*args, **kwargs):
        raise boom

    monkeypatch.setattr(tasks, "run_ingestion", explode)
    seen = {}

    def spy(task, scheduled_job, exc):
        seen["exc"] = exc
        return "handled"

    monkeypatch.setattr(tasks, "_handle_exception", spy)

    assert run_task(job) == "handled"
    assert seen["exc"] is boom


def test_a_database_outage_does_not_stop_the_retry_decision(monkeypatch):
    """If the database is what failed, the bookkeeping fails too — say retry anyway.

    Masking the original error with a logging failure would leave the message
    un-acked in a way nobody can diagnose. The one thing that must survive is the
    decision to retry.
    """
    user = make_user()
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)

    class _BrokenManager:
        def filter(self, *args, **kwargs):
            raise OperationalError("database is down")

    class _BrokenModel:
        objects = _BrokenManager()

    monkeypatch.setattr(tasks, "IngestionJob", _BrokenModel)
    task = _RecordingTask(retries=0)

    with pytest.raises(Retry):
        tasks._handle_exception(task, job, OperationalError("database is down"))

    assert task.retry_kwargs["max_retries"] == 5


def test_a_vendor_api_timeout_reaches_the_retry_path():
    """End to end: a flaky vendor API is retried, not failed."""
    requests = pytest.importorskip("requests")
    make_instrument("AAPL")
    user = make_user()
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.API)

    def flaky_fetcher():
        raise requests.exceptions.ConnectTimeout("vendor API timed out")
        yield  # pragma: no cover - makes this a generator

    with pytest.raises(requests.exceptions.ConnectTimeout) as caught:
        run_ingestion(job, source_type=SourceType.API, fetcher=flaky_fetcher)

    assert is_transient(caught.value) is True

    task = _RecordingTask(retries=0)
    with pytest.raises(Retry):
        tasks._handle_exception(task, job, caught.value)

    job.refresh_from_db()
    assert job.status == JobStatus.PENDING
    assert task.retry_kwargs["max_retries"] == 5


# --------------------------------------------------------------------------- #
# A job that runs for twenty minutes: chunked commits, checkpoint, resume
# --------------------------------------------------------------------------- #


def test_a_chunked_load_commits_each_chunk_and_records_progress(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    job, _ = pending_job_with_file(tmp_path, user)
    job.policy = {"chunk_size": 2}
    job.save(update_fields=["policy"])

    run_task(job)

    job.refresh_from_db()
    assert job.status == JobStatus.COMPLETED
    assert job.checkpoint["chunks"] == 3  # six rows, two per chunk
    assert job.checkpoint["row_offset"] == 6
    assert job.rows_processed == 6
    assert PriceRecord.objects.count() == 6


def test_a_crashed_attempt_resumes_from_its_checkpoint(tmp_path):
    """A worker killed at a chunk boundary loses only the uncommitted chunk."""
    make_instrument("AAPL")
    user = make_user()
    job, path = pending_job_with_file(tmp_path, user)
    policy = IngestionPolicy(chunk_size=2, commit_each_chunk=True)

    class KilledWorker(Exception):
        pass

    def die_after_the_first_chunk(progress):
        raise KilledWorker("simulated SIGKILL")

    with pytest.raises(KilledWorker):
        run_ingestion(
            job,
            source_type=SourceType.CSV,
            path=path,
            policy=policy,
            progress=die_after_the_first_chunk,
        )

    job.refresh_from_db()
    assert job.checkpoint["row_offset"] == 2
    assert job.checkpoint["chunks"] == 1
    assert PriceRecord.objects.count() == 2, "the committed chunk survives"

    result = run_ingestion(
        job,
        source_type=SourceType.CSV,
        path=path,
        policy=policy,
        resume=job.checkpoint,
    )

    job.refresh_from_db()
    assert job.status == JobStatus.COMPLETED
    assert result.accepted == 6
    assert PriceRecord.objects.count() == 6, "the first two rows are not re-inserted"


def test_a_resumed_run_continues_the_counts_rather_than_restarting_them(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    job, path = pending_job_with_file(tmp_path, user)
    policy = IngestionPolicy(chunk_size=2, commit_each_chunk=True)

    class KilledWorker(Exception):
        pass

    def die(progress):
        raise KilledWorker

    with pytest.raises(KilledWorker):
        run_ingestion(
            job, source_type=SourceType.CSV, path=path, policy=policy, progress=die
        )

    job.refresh_from_db()
    result = run_ingestion(
        job, source_type=SourceType.CSV, path=path, policy=policy, resume=job.checkpoint
    )

    # 6, not 2 + 6: the resumed counters are the job's totals, not this attempt's.
    assert result.job.rows_received == 6
    assert result.job.rows_accepted == 6
    assert job.validation_errors.count() == 0


def test_chunked_commits_are_impossible_to_mix_with_abort_on_invalid():
    """The two promises are contradictory, so the combination is refused."""
    with pytest.raises(ValueError, match="abort"):
        IngestionPolicy(on_invalid="abort", commit_each_chunk=True)


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


def test_a_cancelled_job_stops_at_a_chunk_boundary(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    job, path = pending_job_with_file(tmp_path, user)

    def cancel_after_the_first_chunk(progress):
        IngestionJob.objects.filter(pk=job.pk).update(status=JobStatus.CANCELLED)

    result = run_ingestion(
        job,
        source_type=SourceType.CSV,
        path=path,
        policy=IngestionPolicy(chunk_size=2, commit_each_chunk=True),
        progress=cancel_after_the_first_chunk,
        should_cancel=lambda: IngestionJob.objects.filter(
            pk=job.pk, status=JobStatus.CANCELLED
        ).exists(),
    )

    job.refresh_from_db()
    assert result.cancelled is True
    assert job.status == JobStatus.CANCELLED
    assert job.rows_received == 2, "the load stops where it was asked to"
    assert PriceRecord.objects.count() == 2, "what was committed stays committed"


def test_cancelling_a_finished_job_is_a_no_op():
    from apps.ingestion.services import cancel_job

    user = make_user()
    job = IngestionJob.objects.create(
        created_by=user, source_type=SourceType.CSV, status=JobStatus.COMPLETED
    )

    assert cancel_job(job).status == JobStatus.COMPLETED


def test_cancelling_a_queued_job_is_enough_to_stop_it():
    from apps.ingestion.services import cancel_job

    user = make_user()
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)

    assert cancel_job(job).status == JobStatus.CANCELLED
    assert run_task(job) == f"skipped:{JobStatus.CANCELLED}"


# --------------------------------------------------------------------------- #
# Only some rows are valid
# --------------------------------------------------------------------------- #


def test_a_partly_valid_file_completes_with_errors(tmp_path):
    """A bad row is a finding, not a failure — the job finishes and reports it."""
    make_instrument("AAPL")
    user = make_user()
    rows = rows_for(TRADING_DAYS[:2]) + [
        ["AAPL", "2024-03-20", "100.00", "102.00", "99.00", "101.00", "101.00", -5, "async"],
    ]
    job, _ = pending_job_with_file(tmp_path, user, rows=rows, name="mixed.csv")

    outcome = run_task(job)

    job.refresh_from_db()
    assert outcome == f"{JobStatus.COMPLETED_WITH_ERRORS}:{job.pk}"
    assert (job.rows_received, job.rows_accepted, job.rows_rejected) == (3, 2, 1)
    assert job.validation_errors.count() == 1
    assert PriceRecord.objects.count() == 2


# --------------------------------------------------------------------------- #
# Stale-job detection and recovery
# --------------------------------------------------------------------------- #


def _processing_job(user, *, heartbeat=None, attempts=0, started=None):
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)
    IngestionJob.objects.filter(pk=job.pk).update(
        status=JobStatus.PROCESSING,
        heartbeat_at=heartbeat,
        started_at=started,
        attempts=attempts,
    )
    job.refresh_from_db()
    return job


def test_stale_jobs_are_only_those_with_a_cold_heartbeat():
    user = make_user()
    long_ago = timezone.now() - timedelta(hours=1)
    abandoned = _processing_job(user, heartbeat=timezone.now() - timedelta(hours=1))
    healthy = _processing_job(user, heartbeat=timezone.now())
    never_started = _processing_job(user, heartbeat=None, started=long_ago)

    stale = set(monitoring.stale_jobs().values_list("pk", flat=True))

    assert stale == {abandoned.pk, never_started.pk}
    assert healthy.pk not in stale


def test_requeue_stale_jobs_returns_an_abandoned_job_to_pending():
    user = make_user()
    job = _processing_job(user, heartbeat=timezone.now() - timedelta(hours=1))

    call_command("requeue_stale_jobs", "--no-requeue")

    job.refresh_from_db()
    assert job.status == JobStatus.PENDING, "it is claimable again"
    assert job.checkpoint == {}, "and its checkpoint is untouched, for a resume"


def test_requeue_stale_jobs_fails_a_job_that_exhausted_its_attempts(settings):
    settings.INGESTION_MAX_RETRIES = 2
    user = make_user()
    job = _processing_job(
        user, heartbeat=timezone.now() - timedelta(hours=1), attempts=3
    )

    call_command("requeue_stale_jobs")

    job.refresh_from_db()
    assert job.status == JobStatus.FAILED
    assert "abandoned" in job.last_error


def test_requeue_stale_jobs_dry_run_changes_nothing():
    user = make_user()
    job = _processing_job(user, heartbeat=timezone.now() - timedelta(hours=1))

    call_command("requeue_stale_jobs", "--dry-run")

    job.refresh_from_db()
    assert job.status == JobStatus.PROCESSING


def test_requeue_stale_jobs_leaves_a_healthy_job_alone():
    user = make_user()
    job = _processing_job(user, heartbeat=timezone.now())

    call_command("requeue_stale_jobs", "--no-requeue")

    job.refresh_from_db()
    assert job.status == JobStatus.PROCESSING


def test_requeue_stale_jobs_survives_the_broker_being_down(monkeypatch):
    """The broker being down must not lose the recovery: the row is already pending."""
    from apps.ingestion import services

    user = make_user()
    job = _processing_job(user, heartbeat=timezone.now() - timedelta(hours=1))

    def broker_is_down(_job):
        raise ConnectionError("could not connect to redis")

    monkeypatch.setattr(services, "enqueue_ingestion_job", broker_is_down)

    call_command("requeue_stale_jobs")

    job.refresh_from_db()
    assert job.status == JobStatus.PENDING, "recovery happened even though the queue did not"
