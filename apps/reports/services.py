"""Read-only reporting over rows other apps own.

This app deliberately has no models. A report is an *aggregate* of rows that
already exist -- ``IngestionJob`` and ``AuditEvent`` -- so there is nothing here
to migrate and nothing here to keep in step with a schema. Each function is a
queryset plus arithmetic, which is what the service layer is for.

The one report that returns rows rather than figures is the audit log; it is a
plain read of ``AuditEvent`` and is paginated by DRF rather than aggregated here.

The one rule that shapes every query in this module: **never join two one-to-many
relationships into the same aggregate.** ``IngestionJob`` has three children
(``files``, ``validation_errors``, ``rule_summaries``), and joining any two of
them multiplies the rows before ``Count``/``Sum`` sees them -- a job with two
files and three validation errors would be counted six times. So the report reads
the counters stored on the job row and groups one dimension at a time, and it
never fans a job out across its children. ``test_user_analytics`` pins that down
with a test that makes the naive query wrong on purpose.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import (
    Avg,
    Count,
    DurationField,
    ExpressionWrapper,
    F,
    Max,
    Q,
    Sum,
)
from django.db.models.functions import TruncDate

from apps.audit.services import audit_event_queryset
from apps.ingestion.models import IngestionJob, IngestionValidationError, JobStatus

#: The three outcomes the headline buckets name. They are ``JobStatus`` members
#: rather than string literals so a rename in the model cannot silently empty a
#: bucket here. Everything else -- ``pending``, ``processing``, ``cancelled`` --
#: is not an outcome and is reported only under ``by_status``.
SUCCESSFUL_STATUS = JobStatus.COMPLETED
PARTIALLY_SUCCESSFUL_STATUS = JobStatus.COMPLETED_WITH_ERRORS
FAILED_STATUS = JobStatus.FAILED

#: How many rows the two lists in the platform report carry. A report is a
#: summary, not an export: the full set is a paginated read (the audit log), and a
#: top-ten answers "who and what is failing" without turning this into one.
TOP_USERS = 10
FAILURE_SAMPLE = 10


def _duration_expression():
    """``completed_at - started_at`` as a duration, computed in the database.

    Shared by both reports so the one place a run time can be defined is the one
    place it is subtracted. On a job that never started the expression is NULL,
    which ``Avg`` and ``Max`` skip rather than count as zero.
    """
    return ExpressionWrapper(
        F("completed_at") - F("started_at"), output_field=DurationField()
    )



def _jobs_for(user):
    """The caller's own jobs, in one filtered queryset.

    Owner isolation lives in the query, not in the view: a report that scopes in
    Python can be handed another user's rows by a later change, whereas a report
    that scopes in ``filter(created_by=user)`` cannot.
    """
    return IngestionJob.objects.filter(created_by=user)


def _seconds(value: timedelta | None) -> float | None:
    """A ``timedelta`` from the database as JSON-friendly seconds.

    ``None`` in, ``None`` out -- an aggregate over no measurable job is unknown,
    and rendering it as ``0.0`` would overstate the report (an average of zero
    seconds is a fact; an absent average is not).
    """
    if value is None:
        return None
    return round(value.total_seconds(), 3)


def _job_summary(job: IngestionJob | None) -> dict | None:
    """The compact shape the "most recent job" field reports.

    Deliberately not the full job serializer: this is the one job a report quotes,
    so it carries the outcome and the counts and drops the worker bookkeeping
    (checkpoint, heartbeat, policy) that would follow a client around for no
    reason.
    """
    if job is None:
        return None
    return {
        "id": str(job.pk),
        "source_type": job.source_type,
        "status": job.status,
        "requested_at": job.requested_at,
        "completed_at": job.completed_at,
        "rows_received": job.rows_received,
        "rows_accepted": job.rows_accepted,
        "rows_rejected": job.rows_rejected,
    }


def get_user_analytics(user) -> dict:
    """One user's ingestion activity, aggregated in SQL.

    Four grouped reads and one aggregate, on purpose. The alternative -- one
    query that groups by status *and* source *and* day at once -- is not a query;
    a single ``GROUP BY`` produces one grain, and asking for three grains from one
    statement is how a report ends up with rows that multiply. Each read here
    answers exactly one question over the owner's rows.

    Every figure is measured over the caller's jobs only. A user with no jobs gets
    zeroes and empty groupings rather than an error: "no activity yet" is a valid
    answer to every one of these questions.
    """
    jobs = _jobs_for(user)

    # --- the scalar totals: one pass, one row ------------------------------
    # The three outcome counts are conditional aggregation: ``Count`` with a
    # ``filter`` compiles to ``SUM(status = ...)``, so all three come back from a
    # single scan instead of three queries. Duration is ``completed_at -
    # started_at`` computed in the database; a job that never started subtends a
    # NULL, which both ``Avg`` and ``Max`` ignore rather than treat as zero.
    duration = _duration_expression()
    totals = jobs.aggregate(
        total_jobs=Count("id"),
        successful=Count("id", filter=Q(status=SUCCESSFUL_STATUS)),
        partially_successful=Count("id", filter=Q(status=PARTIALLY_SUCCESSFUL_STATUS)),
        failed=Count("id", filter=Q(status=FAILED_STATUS)),
        # rows_received, not rows_processed. ``rows_received`` is written by both
        # the synchronous pipeline and the worker; ``rows_processed`` is the
        # worker's progress counter and stays 0 on a synchronous run, so summing
        # it would under-report every sync load. This is the single most
        # important field choice in the report.
        records_processed=Sum("rows_received"),
        records_accepted=Sum("rows_accepted"),
        records_rejected=Sum("rows_rejected"),
        # Jobs that have both timestamps, i.e. the population the two duration
        # figures are actually measured over. Reported beside them so an average
        # is never read without knowing how many jobs it came from.
        measured_jobs=Count(
            "id", filter=Q(started_at__isnull=False, completed_at__isnull=False)
        ),
        average_seconds=Avg(duration),
        maximum_seconds=Max(duration),
    )

    # --- one dimension per read --------------------------------------------
    # ``values(...).annotate(Count("id"))`` is a GROUP BY that counts jobs, not a
    # join: the count is over the job table alone, so it cannot be inflated by a
    # job that happens to have several files or validation errors.
    by_status = {
        row["status"]: row["count"]
        for row in jobs.values("status").annotate(count=Count("id"))
    }
    by_source = {
        row["source_type"]: row["count"]
        for row in jobs.values("source_type").annotate(count=Count("id"))
    }

    # Daily counts group by the calendar date of ``requested_at``. ``TruncDate``
    # runs in the database's timezone handling; with ``USE_TZ`` the values are the
    # UTC dates the timestamps carry, and a job at 23:59Z belongs to that day, not
    # the next one.
    daily_job_counts = [
        {"date": row["day"], "count": row["count"]}
        for row in jobs.annotate(day=TruncDate("requested_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    ]

    most_recent_job = _job_summary(jobs.order_by("-requested_at").first())

    return {
        "user": {"id": user.pk, "email": user.email},
        "totals": {
            "jobs": totals["total_jobs"] or 0,
            "successful": totals["successful"] or 0,
            "partially_successful": totals["partially_successful"] or 0,
            "failed": totals["failed"] or 0,
        },
        "records": {
            "processed": totals["records_processed"] or 0,
            "accepted": totals["records_accepted"] or 0,
            "rejected": totals["records_rejected"] or 0,
        },
        "durations": {
            "measured_jobs": totals["measured_jobs"] or 0,
            "average_seconds": _seconds(totals["average_seconds"]),
            "maximum_seconds": _seconds(totals["maximum_seconds"]),
        },
        "most_recent_job": most_recent_job,
        "daily_job_counts": daily_job_counts,
        "by_status": by_status,
        "by_source": by_source,
    }


# --------------------------------------------------------------------------- #
# Platform analytics (administrator only)
# --------------------------------------------------------------------------- #


def _failure_summary(job: IngestionJob) -> dict:
    """One failed job, with the line that says why it died.

    ``last_error`` is the infrastructure failure -- why the attempt stopped -- and
    ``error_summary`` is the one-line data-quality verdict. A failed job may carry
    either, so both are reported rather than guessing which one a client wants.
    """
    return {
        "id": str(job.pk),
        "created_by_email": job.created_by.email,
        "source_type": job.source_type,
        "status": job.status,
        "requested_at": job.requested_at,
        "completed_at": job.completed_at,
        "rows_received": job.rows_received,
        "rows_accepted": job.rows_accepted,
        "rows_rejected": job.rows_rejected,
        "last_error": job.last_error,
        "error_summary": job.error_summary,
    }


def get_platform_analytics() -> dict:
    """Ingestion aggregates over every job, for authorized administrators.

    The same measurements as the per-user report -- outcome buckets, record
    totals, run times, groupings, daily counts -- taken over the whole table
    rather than one owner's, plus the two things only a platform view answers:
    which users are generating the work, and what is failing.

    There is no owner filter here, and that is the point. The scope is not a
    query predicate but a permission: the endpoint is guarded by
    ``IsAdministrator``, and this function is only reachable through it. A service
    that silently scoped itself to a user would not be a platform report.

    Failure is summarised three ways because "the platform had 3 failures" is only
    the start of the question: ``recent`` names them, and ``top_error_codes`` says
    which validation rule is doing the rejecting.
    """
    jobs = IngestionJob.objects.all()

    duration = _duration_expression()
    totals = jobs.aggregate(
        total_jobs=Count("id"),
        # Distinct owners, not a join count: the platform report's first question
        # is "how many people use this", which is a count of users, not of jobs.
        users_with_jobs=Count("created_by", distinct=True),
        successful=Count("id", filter=Q(status=SUCCESSFUL_STATUS)),
        partially_successful=Count("id", filter=Q(status=PARTIALLY_SUCCESSFUL_STATUS)),
        failed=Count("id", filter=Q(status=FAILED_STATUS)),
        # In flight or waiting to be. Not an outcome, so it is its own bucket
        # rather than folded into any of the three above.
        active=Count("id", filter=Q(status__in=JobStatus.active())),
        records_processed=Sum("rows_received"),
        records_accepted=Sum("rows_accepted"),
        records_rejected=Sum("rows_rejected"),
        measured_jobs=Count(
            "id", filter=Q(started_at__isnull=False, completed_at__isnull=False)
        ),
        average_seconds=Avg(duration),
        maximum_seconds=Max(duration),
    )

    by_status = {
        row["status"]: row["count"]
        for row in jobs.values("status").annotate(count=Count("id"))
    }
    by_source = {
        row["source_type"]: row["count"]
        for row in jobs.values("source_type").annotate(count=Count("id"))
    }
    daily_job_counts = [
        {"date": row["day"], "count": row["count"]}
        for row in jobs.annotate(day=TruncDate("requested_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    ]

    # The user leaderboard. Grouping by ``created_by`` joins the owner, and that
    # is a **many-to-one** relation -- every job has exactly one owner -- so it
    # cannot multiply rows the way the one-to-many children of a job would. The
    # conditional counts inside are the same ``SUM(status = ...)`` shape as the
    # platform totals, evaluated per owner instead of once.
    top_users = [
        {
            "user_id": row["created_by_id"],
            "email": row["created_by__email"],
            "jobs": row["jobs"],
            "successful": row["successful"],
            "failed": row["failed"],
        }
        for row in jobs.values("created_by_id", "created_by__email")
        .annotate(
            jobs=Count("id"),
            successful=Count("id", filter=Q(status=SUCCESSFUL_STATUS)),
            failed=Count("id", filter=Q(status=FAILED_STATUS)),
        )
        .order_by("-jobs", "created_by__email")[:TOP_USERS]
    ]

    recent_failures = [
        _failure_summary(job)
        for job in jobs.filter(status=FAILED_STATUS)
        # select_related because ``_failure_summary`` reads the owner's email;
        # without it this list is one query plus one per row.
        .select_related("created_by")
        .order_by("-requested_at")[:FAILURE_SAMPLE]
    ]

    # Which validation rule rejected the most rows across every job. This reads
    # the finding table directly -- ``values(...).annotate(Count)`` is a GROUP BY
    # over one table, not a join back into jobs, so the counts are of findings,
    # which is exactly what the question asks.
    top_error_codes = [
        {"error_code": row["error_code"], "count": row["count"]}
        for row in IngestionValidationError.objects.values("error_code")
        .annotate(count=Count("id"))
        .order_by("-count", "error_code")[:FAILURE_SAMPLE]
    ]

    return {
        "totals": {
            "jobs": totals["total_jobs"] or 0,
            "users_with_jobs": totals["users_with_jobs"] or 0,
            "successful": totals["successful"] or 0,
            "partially_successful": totals["partially_successful"] or 0,
            "failed": totals["failed"] or 0,
            "active": totals["active"] or 0,
        },
        "records": {
            "processed": totals["records_processed"] or 0,
            "accepted": totals["records_accepted"] or 0,
            "rejected": totals["records_rejected"] or 0,
        },
        "durations": {
            "measured_jobs": totals["measured_jobs"] or 0,
            "average_seconds": _seconds(totals["average_seconds"]),
            "maximum_seconds": _seconds(totals["maximum_seconds"]),
        },
        "by_status": by_status,
        "by_source": by_source,
        "daily_job_counts": daily_job_counts,
        "top_users": top_users,
        "failures": {
            "failed_jobs": totals["failed"] or 0,
            "recent": recent_failures,
            "top_error_codes": top_error_codes,
        },
    }


# --------------------------------------------------------------------------- #
# Audit log (administrator only)
# --------------------------------------------------------------------------- #


def list_audit_events():
    """The paginated audit log, as a queryset.

    The one report that returns rows rather than aggregates, so it is the one that
    hands the view a queryset instead of a dict -- DRF paginates and serializes it
    the same way as every other list endpoint. The queryset itself is owned by
    ``apps.audit`` (it is a read of that app's table, with its ``select_related``);
    this function is the reporting surface's name for it.
    """
    return audit_event_queryset()
