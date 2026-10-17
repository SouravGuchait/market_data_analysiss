"""The ingestion pipeline: source -> validate -> transform -> store.

One job, one call. :func:`run_ingestion` is the whole engine:

1. open the source and resolve its columns (structural errors fail the job here);
2. stream rows, normalise each, and validate it;
3. keep clean rows, record the reasons for dirty ones;
4. flush clean rows to the database, de-duplicated, in chunks;
5. after loading, check expected trading days against the calendar;
6. write the validation errors, the per-rule PASS/FAIL summaries, and the job's
   final counts.

Transaction design
------------------
By default the read/validate/flush phase runs inside a **single**
``transaction.atomic()`` block, which gives one predictable rule: *a job that
ends* ``failed`` *has loaded nothing.* Chunking bounds memory, not the
transaction. Validation errors and summaries are written *after* that block so
they survive a rollback and explain why the job failed.

That is the right default for a file a request can wait for. It is the wrong
shape for a job that may run for twenty minutes on a worker, because it holds
one write transaction open for the whole load. ``IngestionPolicy.commit_each_chunk``
switches to the other shape: every chunk is flushed, its errors written, and a
checkpoint recorded in **its own** transaction. A crash then loses only the
uncommitted chunk, and :func:`run_ingestion` can resume from the checkpoint via
``resume=`` — the ordered ``row_offset`` the previous attempt committed. The two
modes are mutually exclusive with ``on_invalid="abort"`` for the obvious
reason: you cannot roll back a batch that is already half-committed.

Failure policy
--------------
``policy.on_invalid`` chooses between the two defensible answers for a batch that
is partly bad:

* ``"skip"`` (default) — keep every valid row, record the rest, and finish
  ``completed_with_errors``. The job is worth something.
* ``"abort"`` — one bad row rolls the whole batch back and the job ends
  ``failed``. The load is all-or-nothing.

Duplicate policy is separate and lives on persistence (``on_existing``).

Memory
------
The read loop is streaming: rows are validated one at a time, and only a bounded
``chunk_size`` buffer is ever held. Two things historically broke that promise,
and both are now bounded by the *shape* of the data rather than its size:

* the calendar-gap check used to accumulate every trading date it saw, which is
  one ``date`` object per record — a structure that grows with the file; and
* the checkpoint used to carry that structure, so a chunked run rewrote a
  payload that grew with every chunk — write amplification proportional to
  ``rows² / chunk_size``.

Both are replaced by a :class:`_Coverage`: two ordinals and a symbol list, with
the days actually stored read back from the price table by indexed range scan.
The checkpoint is then a fixed size, whatever the file.
"""

from __future__ import annotations

import contextlib
import logging
from collections import Counter
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
from apps.marketdata.models import PriceRecord

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
    """The decisions a load must make, stated explicitly.

    ``on_invalid`` is ``"skip"`` (load the good rows, report the rest) or
    ``"abort"`` (all-or-nothing). ``on_existing`` decides what a repeat of an
    (instrument, date) already in the table means: ``"update"``, ``"skip"``, or
    ``"fail"``. ``max_errors`` caps how many failures are stored so one hostile
    file cannot fill the error table; the overflow is counted, not silently lost.

    ``commit_each_chunk`` selects the transaction shape. ``False`` (the default,
    and what the synchronous request path uses) wraps the whole load in one
    transaction. ``True`` commits per chunk and checkpoints, which is what the
    worker uses so a long load does not pin one transaction for its duration.
    """

    on_invalid: str = "skip"
    on_existing: str = "update"
    chunk_size: int = 1000
    max_errors: int = 1000
    run_calendar_check: bool = True
    commit_each_chunk: bool = False

    def __post_init__(self) -> None:
        if self.on_invalid not in {"skip", "abort"}:
            raise ValueError(f"on_invalid must be 'skip' or 'abort', got {self.on_invalid!r}")
        if self.on_existing not in {"update", "skip", "fail"}:
            raise ValueError(
                f"on_existing must be 'update', 'skip' or 'fail', got {self.on_existing!r}"
            )
        if self.commit_each_chunk and self.on_invalid == "abort":
            # Contradictory: "abort" promises the batch is all-or-nothing, but a
            # chunked load has already committed earlier chunks by the time a bad
            # row is seen. Reject the combination rather than quietly weaken one.
            raise ValueError(
                "commit_each_chunk=True cannot be combined with on_invalid='abort'"
            )


@dataclass(frozen=True)
class Progress:
    """A snapshot handed to the ``progress`` callback after each chunk.

    Deliberately carries the checkpoint payload too, so the callback that writes
    progress and the checkpoint that enables a resume are the same object and
    cannot disagree.
    """

    received: int
    accepted: int
    rejected: int
    chunks: int
    checkpoint: dict[str, Any]


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
    cancelled: bool = False
    report: dict = field(default_factory=dict)


@dataclass
class _Coverage:
    """What a run loaded, summarised instead of accumulated.

    The calendar check asks exactly one question per symbol: inside the range this
    batch covers, which expected trading days did the file contain no row for?
    Membership is therefore the only operation needed — not a list of the dates —
    so each symbol's dates are held as a :class:`_ObservedDates` bitmap: one bit
    per calendar day rather than one ``date`` object per record.

    The span and the symbol names also make the checkpoint a fixed size, instead
    of one that grows with the row count and is rewritten on every chunk.
    """

    first: date | None = None
    last: date | None = None
    symbols: set[str] = field(default_factory=set)
    dates: dict[str, _ObservedDates] = field(default_factory=dict)

    def add(self, symbol: str, day: date) -> None:
        self.symbols.add(symbol)
        self.dates.setdefault(symbol, _ObservedDates()).add(day)
        if self.first is None or day < self.first:
            self.first = day
        if self.last is None or day > self.last:
            self.last = day

    @property
    def empty(self) -> bool:
        return not self.symbols

    def observed(self, symbol: str) -> set[date]:
        """The dates this run loaded for one symbol, as a plain set."""
        bitmap = self.dates.get(symbol)
        return bitmap.days() if bitmap is not None else set()

    @property
    def nbytes(self) -> int:
        """Total bytes held for the observed dates across every symbol."""
        return sum(bitmap.nbytes for bitmap in self.dates.values())

    def payload(self) -> dict[str, Any]:
        """The resume-safe form: fixed size, independent of the row count.

        The dates are deliberately absent. They are re-derived on resume from the
        rows this job already wrote (see :meth:`seed_from_storage`), which is both
        cheaper than carrying a bitmap per chunk and the only source that is
        guaranteed to match what was committed.
        """
        return {
            "first": self.first.isoformat() if self.first else None,
            "last": self.last.isoformat() if self.last else None,
            "symbols": sorted(self.symbols),
        }

    @classmethod
    def restore(
        cls, stored: dict[str, Any] | None, legacy_observed: dict | None = None
    ) -> _Coverage:
        """Rebuild the span from a checkpoint; the dates come later.

        ``legacy_observed`` is the format this checkpoint used before the dates
        were dropped, and is still honoured so a checkpoint written by an older
        revision resumes rather than silently restarting the load.
        """
        coverage = cls()
        if stored and stored.get("first"):
            coverage.first = date.fromisoformat(stored["first"])
            coverage.last = date.fromisoformat(stored["last"])
            coverage.symbols = set(stored.get("symbols") or [])
        for symbol, days in (legacy_observed or {}).items():
            for day in days:
                coverage.add(symbol, date.fromisoformat(day))
        return coverage

    def seed_from_storage(self, job: IngestionJob) -> None:
        """Re-derive the dates a previous attempt loaded, from the price table.

        Only needed for a resume: a fresh run knows its own dates because it just
        read them. Rows are matched on ``ingestion_job``, so this is what *this
        file* contributed — the same scope the check uses for a single run — and
        the query streams, so it costs nothing per row beyond the bitmap bit.

        One asymmetry is worth naming: under ``on_existing="skip"`` a row that
        already existed keeps its original job id, so it is not attributed here
        and can surface as a missing trading day. That matches what the policy
        asked for — the row was deliberately not loaded by this job — and the
        worker does not use ``skip``.
        """
        if not (self.first and self.last):
            return
        rows = (
            PriceRecord.objects.filter(
                ingestion_job=job,
                trading_date__gte=self.first,
                trading_date__lte=self.last,
            )
            .values_list("instrument__symbol", "trading_date")
            .iterator(chunk_size=2000)
        )
        for symbol, day in rows:
            self.add((symbol or "").strip().upper(), day)


class _ObservedDates:
    """The trading dates one symbol contributed, as a bitmap over its own range.

    Membership is the only operation the calendar check needs, so a bit per day is
    all the state required. A ``set`` of ``date`` objects costs roughly 50 bytes
    per *record*, which for a multi-million-row file is the difference between
    loading it and not; this costs one bit per *day*, so a decade of data for one
    instrument is under a kilobyte.

    The base is kept byte-aligned so that growing the map backwards — a file that
    is not in date order — is a byte-wise prepend rather than a bit shift.
    """

    __slots__ = ("_base", "_bits")

    #: Bytes added when the map has to grow. A little headroom makes both
    #: directions amortised, even for a source that arrives newest-first.
    _GROWTH = 8

    def __init__(self) -> None:
        self._base = 0
        self._bits = bytearray()

    def add(self, day: date) -> None:
        ordinal = day.toordinal()
        if not self._bits:
            growth = self._GROWTH
            self._base = ordinal - (ordinal % growth)
            self._bits = bytearray(growth)
        elif ordinal < self._base:
            growth = max((self._base - ordinal + 7) // 8, self._GROWTH)
            self._bits = bytearray(growth) + self._bits
            self._base -= growth * 8

        offset = ordinal - self._base
        index = offset >> 3
        if index >= len(self._bits):
            self._bits.extend(bytes(index - len(self._bits) + self._GROWTH))
        self._bits[index] |= 1 << (offset & 7)

    def __contains__(self, day: date) -> bool:
        offset = day.toordinal() - self._base
        if offset < 0 or (offset >> 3) >= len(self._bits):
            return False
        return bool(self._bits[offset >> 3] & (1 << (offset & 7)))

    def __len__(self) -> int:
        return sum(byte.bit_count() for byte in self._bits)

    @property
    def nbytes(self) -> int:
        """Bytes held for this symbol: one bit per day, plus growth headroom.

        Exposed so the memory claim in the class docstring is testable rather
        than merely asserted in prose.
        """
        return len(self._bits)

    def days(self) -> set[date]:
        """The observed dates, materialised. Bounded by the calendar span, not the
        row count, and only ever asked for one symbol at a time."""
        out: set[date] = set()
        for index, byte in enumerate(self._bits):
            while byte:
                lowest = byte & -byte
                bit = lowest.bit_length() - 1
                out.add(date.fromordinal(self._base + (index << 3) + bit))
                byte ^= lowest
        return out


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
    progress: Callable[[Progress], None] | None = None,
    resume: dict[str, Any] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> IngestionResult:
    """Run one ingestion and return its result.

    Contract
    --------
    in : the (already created, ``pending``) job, a ``SourceType`` value, whatever
         that source needs (a ``path`` or a ``fetcher``), and an
         :class:`IngestionPolicy`. The three optional extras are for the worker:

         * ``progress`` — called after each committed chunk with a
           :class:`Progress` snapshot;
         * ``resume`` — a previous attempt's checkpoint, whose ``row_offset``
           rows are skipped and whose counters are continued;
         * ``should_cancel`` — polled at each chunk boundary; ``True`` finishes
           the job as ``cancelled``.

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

    job.status = JobStatus.PROCESSING
    job.started_at = job.started_at or timezone.now()
    job.heartbeat_at = timezone.now()
    job.save(update_fields=["status", "started_at", "heartbeat_at"])

    symbols = SymbolIndex.load()
    context = RuleContext(known_symbols=symbols.known_symbols)
    tally = validation.ValidationTally()

    received = 0
    errors: list[IngestionValidationError] = []
    truncated = 0
    persisted = PersistResult()
    coverage = _Coverage()
    accepted_buffer: list[NormalizedRow] = []
    chunks = 0
    row_offset = 0
    total = None
    cancelled = False

    # Continue a previous attempt: restore the counters it committed and skip
    # the rows it already handled. Only ever supplied for an ordered source.
    if resume:
        received = int(resume.get("received", 0))
        row_offset = int(resume.get("row_offset", 0))
        chunks = int(resume.get("chunks", 0))
        truncated = int(resume.get("truncated", 0))
        total = resume.get("total")
        tally.evaluated = received
        tally.accepted = int(resume.get("accepted", 0))
        tally.rejected = int(resume.get("rejected", 0))
        tally.failures = Counter(resume.get("failures") or {})
        coverage = _Coverage.restore(resume.get("coverage"), resume.get("observed"))
        if coverage.symbols:
            # The checkpoint no longer carries per-date data, so the dates a
            # previous attempt loaded are read back from the rows it wrote.
            coverage.seed_from_storage(job)
        persisted = PersistResult(
            created=int(resume.get("created", 0)),
            updated=int(resume.get("updated", 0)),
            skipped=int(resume.get("skipped", 0)),
        )

    def checkpoint_payload() -> dict[str, Any]:
        return {
            "row_offset": row_offset,
            "received": received,
            "accepted": tally.accepted,
            "rejected": tally.rejected,
            "chunks": chunks,
            "created": persisted.created,
            "updated": persisted.updated,
            "skipped": persisted.skipped,
            "truncated": truncated,
            "failures": dict(tally.failures),
            "coverage": coverage.payload(),
            "total": total,
        }

    def record_errors(row: NormalizedRow, outcomes: Iterable[Outcome]) -> None:
        nonlocal truncated
        for outcome in outcomes:
            if len(errors) >= policy.max_errors:
                truncated += 1
                continue
            errors.append(_to_error(job, row, outcome))

    def commit_chunk(buffer: list[NormalizedRow]) -> None:
        """Flush one chunk; in chunked mode, in its own transaction."""
        nonlocal chunks, persisted, errors
        if policy.commit_each_chunk:
            with transaction.atomic():
                persisted = _merge(persisted, _flush(job, buffer, symbols, policy))
                chunks += 1
                _checkpoint(job, checkpoint_payload(), received, tally)
                if errors:
                    IngestionValidationError.objects.bulk_create(errors)
                    errors = []
        else:
            persisted = _merge(persisted, _flush(job, buffer, symbols, policy))
            chunks += 1
        if progress is not None:
            progress(
                Progress(
                    received=received,
                    accepted=tally.accepted,
                    rejected=tally.rejected,
                    chunks=chunks,
                    checkpoint=checkpoint_payload(),
                )
            )

    try:
        # In chunked mode there is deliberately no outer transaction: each chunk
        # commits on its own, which is what lets a long load be resumed.
        outer = (
            contextlib.nullcontext() if policy.commit_each_chunk else transaction.atomic()
        )
        with outer:
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
                if row_offset and raw_row.number <= row_offset:
                    continue
                received += 1
                row_offset = raw_row.number
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
                    coverage.add(row.symbol.strip().upper(), row.trading_date)
                if len(accepted_buffer) >= policy.chunk_size:
                    commit_chunk(accepted_buffer)
                    accepted_buffer = []
                    if should_cancel is not None and should_cancel():
                        cancelled = True
                        break

            if accepted_buffer:
                commit_chunk(accepted_buffer)
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
    # The window is the span the batch itself covers. A gap is only meaningful
    # inside a range we know was being tracked; flagging the whole calendar
    # would report every non-trading symbol as missing. A trailing day dropped
    # from a single-symbol file is therefore out of scope by design.
    #
    # The comparison is against the dates *this file* contributed, held as a
    # bitmap per symbol. Deliberately not "does the table have a row": a day
    # loaded by an earlier, different job is not this batch's coverage, and
    # reporting it as present would hide a genuine gap in the file.
    calendar_outcomes: list[Outcome] = []
    calendar_checks = 0
    if policy.run_calendar_check and not coverage.empty:
        expected = persistence.expected_calendar_dates(coverage.first, coverage.last)
        if expected:
            for symbol in sorted(coverage.symbols):
                calendar_checks += len(expected)
                calendar_outcomes.extend(
                    missing_trading_dates(
                        symbol=symbol,
                        expected_dates=expected,
                        observed_dates=coverage.observed(symbol),
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

    if cancelled:
        # A cancelled run stops at a chunk boundary. Whatever committed stays
        # committed; the job is terminal, so nothing will run it again.
        with transaction.atomic():
            rows_to_store = list(errors)
            if rows_to_store:
                IngestionValidationError.objects.bulk_create(rows_to_store)
            _finish(
                job,
                status=JobStatus.CANCELLED,
                received=received,
                accepted=accepted,
                rejected=rejected,
                counts=counts,
                missing_trading_days=len(calendar_outcomes),
                truncated_errors=truncated,
            )
        logger.info("ingestion job %s cancelled after %s rows", job.pk, received)
        return IngestionResult(
            job=job,
            created=persisted.created,
            updated=persisted.updated,
            skipped=persisted.skipped,
            accepted=accepted,
            rejected=rejected,
            missing_trading_days=len(calendar_outcomes),
            truncated_errors=truncated,
            cancelled=True,
            report=reporting.job_report(job),
        )

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


def _checkpoint(
    job: IngestionJob,
    payload: dict[str, Any],
    received: int,
    tally: validation.ValidationTally,
) -> None:
    """Persist the resume point for a chunked run, inside the chunk's transaction.

    Written on the same commit as the rows it describes, so the checkpoint can
    never claim progress that was rolled back.
    """
    job.checkpoint = payload
    job.rows_processed = received
    job.rows_accepted = tally.accepted
    job.rows_rejected = tally.rejected
    job.heartbeat_at = timezone.now()
    job.save(
        update_fields=[
            "checkpoint",
            "rows_processed",
            "rows_accepted",
            "rows_rejected",
            "heartbeat_at",
        ]
    )


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

    ``failed`` when nothing usable arrived, ``completed_with_errors`` when the
    load is complete for what arrived but something was rejected or an expected
    trading day is absent, otherwise ``completed``.
    """
    if received == 0 or accepted == 0:
        return JobStatus.FAILED
    if rejected or missing_trading_days:
        return JobStatus.COMPLETED_WITH_ERRORS
    return JobStatus.COMPLETED
