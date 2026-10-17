"""The synchronous ingestion pipeline: source -> validate -> transform -> store.

One job, one call, one transaction. :func:`run_ingestion` is the whole engine:

1. open the source and resolve its columns (structural errors fail the job here);
2. stream rows, normalise each, and validate it;
3. keep clean rows, record the reasons for dirty ones;
4. flush clean rows to the database, de-duplicated, in chunks;
5. after loading, check expected trading days against the calendar;
6. write the validation errors, the per-rule PASS/FAIL summaries, and the job's
   final counts.

Transaction design
------------------
The read/validate/flush phase runs inside a single ``transaction.atomic()``
block. That gives one predictable rule: **a job that ends ``failed`` has loaded
nothing.** Chunking bounds memory, not the transaction; a 25 MiB source is a few
hundred thousand rows, which one transaction handles comfortably. Validation
errors and summaries are written *after* that block in their own transaction, so
they survive a rollback and explain why the job failed.

Failure policy
--------------
``policy.on_invalid`` chooses between the two defensible answers for a batch that
is partly bad:

* ``"skip"`` (default) — keep every valid row, record the rest, and finish
  ``partial``. The job is worth something.
* ``"abort"`` — one bad row rolls the whole batch back and the job ends
  ``failed``. The load is all-or-nothing.

Duplicate policy is separate and lives on persistence (``on_existing``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.ingestion.models import (
    IngestionJob,
    IngestionRuleSummary,
    IngestionValidationError,
    JobStatus,
)

from . import persistence, reporting, transform, validation
from .errors import (
    BatchError,
    RuleName,
    SchemaError,
    SourceFormatError,
    UnsafeFileError,
)
from .persistence import PersistResult, SymbolIndex
from .rules import RuleContext, missing_trading_dates
from .schemas import (
    NormalizedRow,
    Outcome,
    missing_required_columns,
    normalize_row,
    resolve_columns,
)
from .sources import read_source

logger = logging.getLogger("market_data")


class _BatchAborted(Exception):
    """Raised to roll the whole batch back under the ``abort`` policy."""


@dataclass
class IngestionPolicy:
    """The two decisions a load must make, stated explicitly.

    ``on_invalid`` is ``"skip"`` (load the good rows, report the rest) or
    ``"abort"`` (all-or-nothing). ``on_existing`` decides what a repeat of an
    (instrument, date) already in the table means: ``"update"``, ``"skip"``, or
    ``"fail"``. ``max_errors`` caps how many failures are stored so one hostile
    file cannot fill the error table; the overflow is counted, not silently lost.
    """

    on_invalid: str = "skip"
    on_existing: str = "update"
    chunk_size: int = 1000
    max_errors: int = 1000
    run_calendar_check: bool = True

    def __post_init__(self) -> None:
        if self.on_invalid not in {"skip", "abort"}:
            raise ValueError(f"on_invalid must be 'skip' or 'abort', got {self.on_invalid!r}")
        if self.on_existing not in {"update", "skip", "fail"}:
            raise ValueError(
                f"on_existing must be 'update', 'skip' or 'fail', got {self.on_existing!r}"
            )


@dataclass
class IngestionResult:
    """The outcome of one run, mirroring the job's stored counts."""

    job: IngestionJob
    created: int = 0
    updated: int = 0
    skipped: int = 0
    accepted: int = 0
    rejected: int = 0
    missing_trading_days: int = 0
    truncated_errors: int = 0
    report: dict = field(default_factory=dict)


def _to_error(job: IngestionJob, row: NormalizedRow, outcome: Outcome) -> IngestionValidationError:
    """Build one validation-error row from a finding, with safe, bounded detail."""
    return IngestionValidationError(
        job=job,
        row_number=row.row_number,
        field_name=(outcome.field or "")[:128],
        error_code=str(outcome.rule)[:64],
        detail=(outcome.detail or "")[:255],
        raw_row=row.raw,
    )


def _batch_error(job: IngestionJob, code: str, detail: str, field_name: str = ""):
    """Build a job-level error row (``row_number = 0``) for a failure with no row."""
    return IngestionValidationError(
        job=job,
        row_number=0,
        field_name=field_name[:128],
        error_code=str(code)[:64],
        detail=detail[:255],
        raw_row=None,
    )


def _flush(
    job: IngestionJob,
    rows: list[NormalizedRow],
    symbols: SymbolIndex,
    policy: IngestionPolicy,
) -> PersistResult:
    """Transform a chunk and upsert it. Runs inside the caller's transaction."""
    if not rows:
        return PersistResult()
    frame = transform.prepare_for_load(transform.to_frame(rows))
    records = transform.frame_to_records(frame)
    return persistence.upsert_prices(
        job=job,
        records=records,
        symbols=symbols,
        on_existing=policy.on_existing,
    )


def _finish(
    job: IngestionJob,
    *,
    status: str,
    received: int,
    accepted: int,
    rejected: int,
    counts: dict[str, tuple[int, int]],
    missing_trading_days: int,
    truncated_errors: int,
) -> None:
    """Persist the final counts, status, and one-line summary on the job."""
    job.status = status
    job.rows_received = received
    job.rows_accepted = accepted
    job.rows_rejected = rejected
    job.completed_at = timezone.now()
    job.error_summary = reporting.render_error_summary(
        received=received,
        accepted=accepted,
        rejected=rejected,
        counts=counts,
        missing_trading_days=missing_trading_days,
        truncated_errors=truncated_errors,
    )
    job.save(
        update_fields=[
            "status",
            "rows_received",
            "rows_accepted",
            "rows_rejected",
            "completed_at",
            "error_summary",
        ]
    )


def _hard_failure(
    job: IngestionJob, batch_errors: list[IngestionValidationError]
) -> IngestionResult:
    """Record a job-level failure and return a zero-row result."""
    with transaction.atomic():
        if batch_errors:
            IngestionValidationError.objects.bulk_create(batch_errors)
        _finish(
            job,
            status=JobStatus.FAILED,
            received=job.rows_received,
            accepted=0,
            rejected=0,
            counts={},
            missing_trading_days=0,
            truncated_errors=0,
        )
    return IngestionResult(job=job, report=reporting.job_report(job))


def run_ingestion(
    job: IngestionJob,
    *,
    source_type: str,
    path: str | Path | None = None,
    fetcher: Callable[[], Iterable[dict[str, Any]]] | None = None,
    sheet_name: str | None = None,
    records_key: str | None = None,
    policy: IngestionPolicy | None = None,
    default_source: str | None = None,
) -> IngestionResult:
    """Run one synchronous ingestion and return its result.

    Contract
    --------
    in : the (already created, ``pending``) job, a ``SourceType`` value, whatever
         that source needs (a ``path`` or a ``fetcher``), and an
         :class:`IngestionPolicy`.
    out: an :class:`IngestionResult`; the job's row is updated in place and
         :func:`apps.ingestion.etl.reporting.job_report` is attached.

    Business purpose: the single entry point that guarantees every ingest —
    whatever the source — leaves the same trail: a job with counts, one rule
    summary per rule, and one stored reason per rejected row.
    """
    policy = policy or IngestionPolicy()
    resolved_default_source = default_source or (
        f"{source_type}:{Path(path).name}" if path else source_type
    )

    job.status = JobStatus.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at"])

    symbols = SymbolIndex.load()
    context = RuleContext(known_symbols=symbols.known_symbols)
    tally = validation.ValidationTally()

    received = 0
    errors: list[IngestionValidationError] = []
    truncated = 0
    persisted = PersistResult()
    observed: dict[str, set[date]] = {}
    accepted_buffer: list[NormalizedRow] = []

    def record_errors(row: NormalizedRow, outcomes: Iterable[Outcome]) -> None:
        nonlocal truncated
        for outcome in outcomes:
            if len(errors) >= policy.max_errors:
                truncated += 1
                continue
            errors.append(_to_error(job, row, outcome))

    try:
        with transaction.atomic():
            reader = read_source(
                source_type,
                path=path,
                fetcher=fetcher,
                sheet_name=sheet_name,
                records_key=records_key,
            )
            columns = resolve_columns(reader.header)
            missing = missing_required_columns(columns)
            if missing:
                raise SchemaError(missing)

            for raw_row in reader.rows:
                received += 1
                row = normalize_row(
                    row_number=raw_row.number,
                    raw=raw_row.data,
                    columns=columns,
                    default_source=resolved_default_source,
                )
                outcomes = validation.validate_row(row, context)
                tally.observe(outcomes)

                if outcomes:
                    record_errors(row, outcomes)
                    if policy.on_invalid == "abort":
                        raise _BatchAborted(row.row_number)
                    continue

                accepted_buffer.append(row)
                if row.symbol and row.trading_date:
                    observed.setdefault(row.symbol.strip().upper(), set()).add(
                        row.trading_date
                    )
                if len(accepted_buffer) >= policy.chunk_size:
                    persisted = _merge(persisted, _flush(job, accepted_buffer, symbols, policy))
                    accepted_buffer = []

            persisted = _merge(persisted, _flush(job, accepted_buffer, symbols, policy))
            accepted_buffer = []

    except SchemaError as exc:
        return _hard_failure(
            job,
            [_batch_error(job, BatchError.MISSING_COLUMN, str(exc), name) for name in exc.missing_columns],
        )
    except (UnsafeFileError, SourceFormatError) as exc:
        return _hard_failure(job, [_batch_error(job, BatchError.MALFORMED_SOURCE, str(exc))])
    except _BatchAborted as stop:
        errors.insert(
            0,
            _batch_error(
                job,
                BatchError.ABORTED_ON_INVALID,
                f"batch aborted at source row {stop.args[0]} because a row was invalid",
            ),
        )
        with transaction.atomic():
            if errors:
                IngestionValidationError.objects.bulk_create(errors)
            _finish(
                job,
                status=JobStatus.FAILED,
                received=received,
                accepted=0,
                rejected=tally.rejected,
                counts=tally.as_rule_counts(),
                missing_trading_days=0,
                truncated_errors=truncated,
            )
        return IngestionResult(
            job=job, rejected=tally.rejected, report=reporting.job_report(job)
        )

    # --- post-load: calendar gap check, then reporting -------------------- #
    # The window is the span the batch itself covers (the union of every
    # symbol's dates). A gap is only meaningful inside a range we know was being
    # tracked; flagging the whole calendar would report every non-trading symbol
    # as missing. A trailing day dropped from a single-symbol file is therefore
    # out of scope by design.
    calendar_outcomes: list[Outcome] = []
    calendar_checks = 0
    if policy.run_calendar_check and observed:
        all_dates = set().union(*observed.values())
        expected = persistence.expected_calendar_dates(min(all_dates), max(all_dates))
        if expected:
            for symbol, dates in observed.items():
                calendar_checks += len(expected)
                calendar_outcomes.extend(
                    missing_trading_dates(
                        symbol=symbol, expected_dates=expected, observed_dates=dates
                    )
                )

    counts = tally.as_rule_counts()
    counts[RuleName.MISSING_TRADING_DATE] = (
        max(calendar_checks - len(calendar_outcomes), 0),
        len(calendar_outcomes),
    )

    duplicates = len(persisted.duplicates)
    accepted = max(tally.accepted - duplicates, 0)
    rejected = tally.rejected + duplicates

    with transaction.atomic():
        rows_to_store = list(errors)
        rows_to_store.extend(
            _cursor_error(job, outcome) for outcome in calendar_outcomes
        )
        if duplicates:
            rows_to_store.extend(
                _batch_error(
                    job,
                    RuleName.DUPLICATE_RECORD,
                    f"{symbol} already has a stored price for {day}",
                    "symbol",
                )
                for symbol, day in persisted.duplicates
            )
        if received == 0:
            rows_to_store.append(
                _batch_error(job, BatchError.EMPTY_SOURCE, "the source contained no data rows")
            )
        if rows_to_store:
            IngestionValidationError.objects.bulk_create(rows_to_store)
        IngestionRuleSummary.objects.bulk_create(
            reporting.build_rule_summaries(job=job, counts=counts)
        )

        status = _status_for(
            received=received,
            accepted=accepted,
            rejected=rejected,
            missing_trading_days=len(calendar_outcomes),
        )
        _finish(
            job,
            status=status,
            received=received,
            accepted=accepted,
            rejected=rejected,
            counts=counts,
            missing_trading_days=len(calendar_outcomes),
            truncated_errors=truncated,
        )

    result = IngestionResult(
        job=job,
        created=persisted.created,
        updated=persisted.updated,
        skipped=persisted.skipped,
        accepted=accepted,
        rejected=rejected,
        missing_trading_days=len(calendar_outcomes),
        truncated_errors=truncated,
        report=reporting.job_report(job),
    )
    logger.info(
        "ingestion job %s finished %s: %s",
        job.pk,
        job.status,
        job.error_summary,
    )
    return result


def _cursor_error(job: IngestionJob, outcome: Outcome) -> IngestionValidationError:
    """A validation error for a finding that has no source row (a calendar gap)."""
    return IngestionValidationError(
        job=job,
        row_number=0,
        field_name=(outcome.field or "")[:128],
        error_code=str(outcome.rule)[:64],
        detail=(outcome.detail or "")[:255],
        raw_row=None,
    )


def _merge(left: PersistResult, right: PersistResult) -> PersistResult:
    """Fold one chunk's result into the running totals."""
    return PersistResult(
        created=left.created + right.created,
        updated=left.updated + right.updated,
        skipped=left.skipped + right.skipped,
        duplicates=left.duplicates + right.duplicates,
    )


def _status_for(*, received: int, accepted: int, rejected: int, missing_trading_days: int) -> str:
    """Decide the job's terminal status from its counts.

    ``failed`` when nothing usable arrived, ``partial`` when the load is complete
    for what arrived but something was rejected or an expected trading day is
    absent, otherwise ``succeeded``.
    """
    if received == 0 or accepted == 0:
        return JobStatus.FAILED
    if rejected or missing_trading_days:
        return JobStatus.PARTIAL
    return JobStatus.SUCCEEDED
