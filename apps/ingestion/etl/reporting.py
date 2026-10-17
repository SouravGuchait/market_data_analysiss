"""Data-quality reporting: turn raw counts into a job-level report.

The report answers three questions an operator actually asks after a load:

* how many rows went in, and how many came out;
* which rules held and which did not;
* what the first few failures looked like, verbatim.

Rule-level PASS/FAIL is stored as one :class:`~apps.ingestion.models.IngestionRuleSummary`
row per rule, and the sample of failures is read back from the existing
validation-error records. Nothing is recomputed at read time, so the same job
always reports the same numbers.
"""

from __future__ import annotations

from apps.ingestion.models import IngestionRuleSummary

from .errors import ROW_RULES


def build_rule_summaries(*, job, counts: dict[str, tuple[int, int]]):
    """Build one summary row per row rule.

    Contract
    --------
    in : the job and ``{rule_name: (passed, failed)}``.
    out: unsaved :class:`IngestionRuleSummary` instances, one per rule in
         :data:`~apps.ingestion.etl.errors.ROW_RULES`, in a stable order.

    Business purpose: a rule with no failures is still recorded, so the report
    can say "checked and held" rather than leaving silence to be interpreted.
    """
    return [
        IngestionRuleSummary(
            job=job,
            rule_name=rule,
            passed=counts.get(rule, (0, 0))[0],
            failed=counts.get(rule, (0, 0))[1],
        )
        for rule in ROW_RULES
    ]


def render_error_summary(
    *,
    received: int,
    accepted: int,
    rejected: int,
    counts: dict[str, tuple[int, int]],
    missing_trading_days: int = 0,
    truncated_errors: int = 0,
) -> str:
    """Render the one-line ``error_summary`` stored on the job.

    Kept short and human-readable: it is what shows up in the admin list and in
    a log line, so it must be scannable rather than exhaustive.
    """
    parts = [f"received={received}", f"accepted={accepted}", f"rejected={rejected}"]
    if missing_trading_days:
        parts.append(f"missing_trading_days={missing_trading_days}")
    if truncated_errors:
        parts.append(f"errors_truncated={truncated_errors}")
    failures = ", ".join(
        f"{rule}={failed}" for rule, (_, failed) in counts.items() if failed
    )
    if failures:
        parts.append(f"failures: {failures}")
    return "; ".join(parts)


def job_report(job) -> dict:
    """Return a serialisable report for a finished job.

    Contract
    --------
    in : an :class:`~apps.ingestion.models.IngestionJob`.
    out: a plain dict — never a queryset — so it can be logged or returned from a
         view without further conversion.

    Business purpose: the single shape a dashboard or an operator reads, so the
         rule table and the failure sample cannot drift apart.
    """
    summaries = list(job.rule_summaries.all())
    failures = list(
        job.validation_errors.order_by("row_number", "error_code").values(
            "row_number", "error_code", "field_name", "detail"
        )[:20]
    )
    return {
        "job_id": str(job.pk),
        "source_type": job.source_type,
        "status": job.status,
        "rows_received": job.rows_received,
        "rows_accepted": job.rows_accepted,
        "rows_rejected": job.rows_rejected,
        "error_summary": job.error_summary,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "rules": [
            {
                "rule": summary.rule_name,
                "passed": summary.passed,
                "failed": summary.failed,
                "status": str(summary.status),
            }
            for summary in summaries
        ],
        "sample_failures": [
            {
                "row_number": failure["row_number"],
                "rule": failure["error_code"],
                "field": failure["field_name"],
                "detail": failure["detail"],
            }
            for failure in failures
        ],
    }
