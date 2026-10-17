"""Operational visibility: queue depth, worker liveness, and stale jobs.

Three different things can be down, and they fail differently. Keeping them
*separate* is the point of this module:

* **Database down** — no job state can be read or written. Workers still pull
  messages off the broker, then die on the first query. Jobs sit in
  ``processing`` with a heartbeat that stops advancing.
* **Broker (Redis) down** — submissions can still create a job row, but the
  message never reaches a queue, so the job sits ``pending`` forever. Nothing is
  lost, because the row is the truth: a sweeper can re-enqueue every pending job
  once the broker returns.
* **Cache (Redis, as a cache) down** — page loads get slower. No job state is
  affected at all, because no job state lives in Redis.

Reporting these as one "Redis is down" is exactly the mistake this module
avoids: the broker and the cache share a server but not a consequence.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.db.models import Count, Q
from django.utils import timezone

from .models import IngestionJob, JobStatus


def stale_cutoff(*, seconds: int | None = None, now=None):
    """The moment before which a heartbeat counts as cold."""
    seconds = (
        seconds
        if seconds is not None
        else int(getattr(settings, "INGESTION_STALE_AFTER_SECONDS", 15 * 60))
    )
    return (now or timezone.now()) - timedelta(seconds=seconds)


def stale_jobs(*, seconds: int | None = None, now=None):
    """Jobs a worker started and then stopped reporting on.

    A ``processing`` job whose heartbeat has gone cold is the signature of a
    worker that was killed — the process no longer exists to finish the job or
    to fail it. Without this query the job would sit in ``processing`` forever
    and never be retried, which is the failure mode that makes people say a
    queue "lost" a job. It did not lose it; nobody was looking.

    ``heartbeat_at`` is checked first because it is the live signal. Rows that
    never got a heartbeat (an older attempt, or a crash between the claim and
    the first chunk) fall back to ``started_at`` and then ``requested_at``.
    """
    cutoff = stale_cutoff(seconds=seconds, now=now)
    return IngestionJob.objects.filter(status=JobStatus.PROCESSING).filter(
        Q(heartbeat_at__lt=cutoff)
        | Q(heartbeat_at__isnull=True, started_at__lt=cutoff)
        | Q(
            heartbeat_at__isnull=True,
            started_at__isnull=True,
            requested_at__lt=cutoff,
        )
    )


def status_counts() -> dict[str, int]:
    """How many jobs are in each status, as a plain dict."""
    rows = IngestionJob.objects.values("status").annotate(count=Count("id"))
    return {row["status"]: row["count"] for row in rows}


def pending_jobs():
    """Jobs waiting to be claimed — the set a broker outage strands."""
    return IngestionJob.objects.filter(status=JobStatus.PENDING)


def broker_queue_name() -> str:
    """The default queue a worker listens on."""
    return getattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "celery")


def queue_depth(queue: str | None = None) -> int | None:
    """Messages waiting on the broker, or ``None`` if the broker is unreachable.

    Read straight from Redis rather than through Celery's ``inspect`` API.
    ``inspect`` broadcasts to *workers*, so with no workers running it reports
    nothing — the same answer it gives for an empty queue. The broker's own list
    length answers the question actually being asked: is work piling up?
    """
    url = getattr(settings, "CELERY_BROKER_URL", None)
    if not url:
        return None
    try:
        import redis
    except Exception:  # pragma: no cover - redis is a declared dependency
        return None
    try:
        client = redis.Redis.from_url(url)
        return int(client.llen(queue or broker_queue_name()))
    except Exception:
        return None


def database_ok() -> bool:
    """Whether the database answers a trivial query."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True
    except Exception:
        return False


def broker_ok() -> bool:
    """Whether the broker answers a ping."""
    url = getattr(settings, "CELERY_BROKER_URL", None)
    if not url:
        return False
    try:
        import redis
    except Exception:  # pragma: no cover - redis is a declared dependency
        return False
    try:
        redis.Redis.from_url(url).ping()
        return True
    except Exception:
        return False


def health() -> dict:
    """A one-call snapshot that keeps the three failure modes distinct.

    Contract
    --------
    in : nothing; reads settings and probes each dependency.
    out: a dict naming each dependency's state, plus the job counts that make a
         stuck queue visible.

    Business purpose: an operator asking "is it broken?" gets "the database is
    down" or "the broker is down" — not a single red light that could be either.
    """
    return {
        "database": "up" if database_ok() else "down",
        "broker": "up" if broker_ok() else "down",
        "queue_depth": queue_depth(),
        "jobs": status_counts(),
        "stale_processing": stale_jobs().count(),
    }
