"""The Celery task that runs one ingestion job.

Four rules, in order of importance.

1. **The database is the source of truth.** The task returns a small string for
   the logs; it is never the answer to "did the load succeed". That answer is
   ``IngestionJob.status``. No result backend, no second store to disagree.

2. **The task is idempotent, because a broker is not exactly-once.** Celery with
   ``acks_late`` gives *at-least-once* delivery: a worker that dies mid-task
   hands its message back, and the job is delivered again. So the same job can
   legitimately reach a worker twice — and it must be safe when it does. Two
   mechanisms carry that weight:

   * :func:`claim_job` — a single conditional ``UPDATE`` that only matches a
     ``pending`` row, so exactly one delivery transitions it to ``processing``;
   * the upsert in persistence — re-processing rows updates them rather than
     duplicating them.

3. **Only transient failures retry.** See :mod:`apps.ingestion.etl.failures`. A
   malformed file is permanent; a dropped database connection is not.

4. **A repeated delivery of a finished job is a no-op.** If the row is already
   terminal, the task returns immediately without touching the data.
"""

from __future__ import annotations

import logging
import os
import random
import time

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db.models import F
from django.utils import timezone

from .etl.failures import is_transient
from .etl.pipeline import IngestionPolicy, Progress, run_ingestion
from .models import IngestionJob, JobStatus, SourceType

logger = logging.getLogger("market_data.tasks")

TASK_NAME = "ingestion.process_job"

# An API source's ``fetcher`` is a Python callable, and a callable cannot travel
# through a queue — only data can. So a queued API job has nothing to fetch with.
# Rather than accept a URL from a client (an SSRF hole) or fail obscurely, the
# worker says this plainly. API ingestion stays on the synchronous path, where
# the caller supplies the fetcher in-process.
API_CANNOT_BE_QUEUED = (
    "API sources cannot be queued: the fetcher is a callable and cannot be "
    "serialised into a message. Run API ingestion through the synchronous "
    "upload/CLI path, or add a stored request descriptor behind an allowlist."
)


# --------------------------------------------------------------------------- #
# Policy: what the submission asked for, frozen on the job row.
# --------------------------------------------------------------------------- #


def policy_for(job: IngestionJob) -> IngestionPolicy:
    """Rebuild the submission's policy, defaulting to the worker-safe shape.

    ``commit_each_chunk`` is on unless the submitter asked for ``abort``:
    chunking is what keeps a twenty-minute load from pinning a single database
    transaction, but it cannot honour all-or-nothing semantics, and a promise
    about the data outranks an optimisation.
    """
    stored = job.policy or {}
    on_invalid = stored.get("on_invalid", "skip")
    return IngestionPolicy(
        on_invalid=on_invalid,
        on_existing=stored.get("on_existing", "update"),
        chunk_size=int(stored.get("chunk_size", 1000)),
        max_errors=int(stored.get("max_errors", 1000)),
        run_calendar_check=bool(stored.get("run_calendar_check", True)),
        commit_each_chunk=on_invalid != "abort",
    )


def sheet_for(job: IngestionJob) -> str | None:
    """The worksheet a stored policy named, if it named one."""
    return (job.policy or {}).get("sheet_name") or None


# --------------------------------------------------------------------------- #
# Retry policy
# --------------------------------------------------------------------------- #


def _max_retries() -> int:
    return int(getattr(settings, "INGESTION_MAX_RETRIES", 5))


def backoff_seconds(
    attempt: int,
    *,
    base: int | None = None,
    cap: int | None = None,
    jitter: bool = True,
    rng=None,
) -> int:
    """Exponential backoff with full jitter, capped.

    Full jitter — a uniform draw from ``[0, ceiling]`` — rather than a fixed
    delay is deliberate. When many jobs fail at once (a database blip), a
    deterministic backoff makes every worker retry in lockstep and re-creates
    the spike that caused the failure. ``jitter=False`` returns the ceiling
    itself, which is what the tests assert against.
    """
    base = int(
        base if base is not None else getattr(settings, "INGESTION_RETRY_BACKOFF_SECONDS", 10)
    )
    cap = int(
        cap
        if cap is not None
        else getattr(settings, "INGESTION_RETRY_BACKOFF_MAX_SECONDS", 900)
    )
    ceiling = min(cap, base * (2 ** max(int(attempt), 0)))
    if not jitter:
        return ceiling
    return (rng or random).randint(0, ceiling)


# --------------------------------------------------------------------------- #
# Claiming, cancellation, and progress
# --------------------------------------------------------------------------- #


def _load_job(job_id: str) -> IngestionJob | None:
    try:
        return IngestionJob.objects.get(pk=job_id)
    except (IngestionJob.DoesNotExist, ValidationError, ValueError, TypeError):
        return None


def claim_job(job_id: str, *, task_id: str = "") -> bool:
    """Atomically take ownership of a ``pending`` job.

    This one statement carries the whole guarantee against duplicate execution:
    a conditional ``UPDATE`` that only matches a ``pending`` row. Two racing
    workers, or the same message delivered twice, both funnel through it, and
    exactly one receives ``True``. There is no read-then-write window to lose,
    which a ``get()`` followed by ``save()`` would have.
    """
    now = timezone.now()
    updated = IngestionJob.objects.filter(
        pk=job_id, status=JobStatus.PENDING
    ).update(
        status=JobStatus.PROCESSING,
        heartbeat_at=now,
        celery_task_id=task_id or "",
        attempts=F("attempts") + 1,
    )
    return bool(updated)


def _is_cancelled(job_id) -> bool:
    """Poll the row for a cancellation request. Called at chunk boundaries."""
    return IngestionJob.objects.filter(
        pk=job_id, status=JobStatus.CANCELLED
    ).exists()


def _source_path(job: IngestionJob) -> str | None:
    """Return an absolute path to the job's source file, if it has one."""
    record = job.files.first()
    if record is None:
        return None
    stored = record.storage_path
    # A local-file job records an absolute path; an upload records a
    # MEDIA_ROOT-relative one that has to be resolved before it can be read.
    if os.path.isabs(stored):
        return stored
    try:
        return default_storage.path(stored)
    except (NotImplementedError, ValueError):  # non-local storage backend
        return stored


def _resume_point(job: IngestionJob) -> dict | None:
    """A previous attempt's checkpoint, when it is safe to continue from it.

    Resuming means skipping the first ``row_offset`` rows, which is only sound if
    re-reading the source yields rows in the same order. That holds for a file we
    opened twice, and is why the worker only runs file-backed sources at all.
    """
    checkpoint = job.checkpoint or {}
    if not checkpoint.get("row_offset"):
        return None
    return checkpoint


class ProgressReporter:
    """Write progress onto the job row, throttled to a heartbeat interval.

    A chunked run already checkpoints its counters inside the chunk transaction.
    This exists for the heartbeat: without it, a source that yields one very
    large chunk would leave ``heartbeat_at`` frozen and look to the stale-job
    sweeper exactly like a dead worker.
    """

    def __init__(self, job_id, *, interval: int | None = None, clock=time.monotonic):
        self.job_id = str(job_id)
        self.interval = (
            interval
            if interval is not None
            else int(getattr(settings, "INGESTION_HEARTBEAT_SECONDS", 5))
        )
        self._clock = clock
        self._last: float | None = None

    def __call__(self, progress: Progress) -> None:
        now = self._clock()
        if self._last is not None and now - self._last < self.interval:
            return
        self._last = now
        IngestionJob.objects.filter(pk=self.job_id).update(
            rows_processed=progress.received,
            rows_accepted=progress.accepted,
            rows_rejected=progress.rejected,
            heartbeat_at=timezone.now(),
        )


# --------------------------------------------------------------------------- #
# Terminal bookkeeping
# --------------------------------------------------------------------------- #


def _mark_failed(job: IngestionJob, detail: str) -> str:
    """Record a permanent failure. Tolerates the database being the failure."""
    try:
        IngestionJob.objects.filter(pk=job.pk).update(
            status=JobStatus.FAILED,
            completed_at=timezone.now(),
            last_error=detail[:2000],
            error_summary=detail[:255],
        )
    except Exception as exc:  # pragma: no cover - the database is unreachable
        logger.error("job_id=%s could not be marked failed: %s", job.pk, exc)
        return f"failed-unrecorded:{job.pk}"
    return f"failed:{job.pk}"


def _handle_exception(task, job: IngestionJob, exc: Exception) -> str:
    """Decide between a retry and giving up, and record the decision."""
    transient = is_transient(exc)
    retries = int(getattr(task.request, "retries", 0) or 0)
    max_retries = _max_retries()
    detail = f"{type(exc).__name__}: {exc}"

    if transient and retries < max_retries:
        countdown = backoff_seconds(retries)
        logger.warning(
            "job_id=%s transient failure attempt=%s/%s error=%s retry_in=%ss",
            job.pk,
            retries + 1,
            max_retries,
            detail,
            countdown,
        )
        # Hand the job back so the retry can claim it again (claim_job only
        # matches `pending`). If the database is what failed, this write fails
        # too — swallow it: the message is un-acked, so the broker will
        # redeliver, and the stale-job sweeper recovers the row once the
        # database is back. Bookkeeping must never mask the real error.
        try:
            IngestionJob.objects.filter(pk=job.pk).update(
                status=JobStatus.PENDING, last_error=detail[:2000]
            )
        except Exception as bookkeeping_error:  # pragma: no cover
            logger.warning(
                "job_id=%s could not record retry state: %s", job.pk, bookkeeping_error
            )
        raise task.retry(exc=exc, countdown=countdown, max_retries=max_retries)

    reason = (
        "transient failure with no retries left" if transient else "permanent failure"
    )
    logger.error(
        "job_id=%s %s error=%s attempts=%s", job.pk, reason, detail, job.attempts
    )
    return _mark_failed(job, detail)


# --------------------------------------------------------------------------- #
# The task
# --------------------------------------------------------------------------- #


@shared_task(
    bind=True,
    name=TASK_NAME,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_ingestion_job(self, job_id: str) -> str:
    """Load the ingestion job identified by ``job_id``.

    Returns a short ``"<outcome>:<job id>"`` string for the logs. The return
    value is *not* the business result — ``IngestionJob.status`` is.
    """
    job = _load_job(job_id)
    if job is None:
        logger.warning("task received for unknown job_id=%s; ignoring", job_id)
        return f"missing:{job_id}"

    if job.is_terminal:
        # A redelivered message for a finished job. Doing nothing is the point.
        logger.info(
            "job_id=%s already status=%s; repeated delivery is a no-op",
            job_id,
            job.status,
        )
        return f"skipped:{job.status}"

    if not claim_job(job_id, task_id=getattr(self.request, "id", "") or ""):
        # Another worker owns it, or it was cancelled between the load and here.
        logger.info(
            "job_id=%s was not pending; repeated delivery is a no-op", job_id
        )
        return "skipped:claimed"

    job.refresh_from_db()

    if job.source_type == SourceType.API:
        return _mark_failed(job, API_CANNOT_BE_QUEUED)

    path = _source_path(job)
    if path is None:
        return _mark_failed(job, "no source file is attached to this job")

    try:
        result = run_ingestion(
            job,
            source_type=job.source_type,
            path=path,
            sheet_name=sheet_for(job),
            policy=policy_for(job),
            progress=ProgressReporter(job.pk),
            resume=_resume_point(job),
            should_cancel=lambda: _is_cancelled(job.pk),
        )
    except Exception as exc:  # noqa: BLE001 - classified below, deliberately broad
        return _handle_exception(self, job, exc)

    logger.info(
        "job_id=%s finished status=%s received=%s accepted=%s rejected=%s attempts=%s",
        job.pk,
        job.status,
        result.job.rows_received,
        result.job.rows_accepted,
        result.job.rows_rejected,
        job.attempts,
    )
    return f"{job.status}:{job.pk}"
