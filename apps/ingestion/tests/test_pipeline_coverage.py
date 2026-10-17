"""The invariants that decide whether a huge file is loadable at all.

Nothing here is a micro-benchmark. Every test asserts the *shape* of the state a
run carries, because that is what a functional test can never catch: a structure
that quietly grows with the row count, or a checkpoint that quietly grows with
every chunk, breaks no assertion until someone points a million-row file at it.

Two historical regressions are pinned here:

* the calendar-gap check held one ``date`` object per record, so a large file
  needed memory proportional to its size rather than to its date *span*; and
* the checkpoint carried that structure, so a chunked run rewrote a payload that
  grew with every chunk — write amplification of ``rows² / chunk_size``.
"""

import json
from datetime import date, timedelta

import pytest

from apps.ingestion.etl.pipeline import (
    IngestionPolicy,
    _Coverage,
    _ObservedDates,
    run_ingestion,
)
from apps.ingestion.models import IngestionJob, JobStatus, SourceType
from apps.marketdata.models import PriceRecord
from apps.ingestion.services import ingest_file

from .helpers import add_calendar, make_instrument, make_user, write_csv

pytestmark = pytest.mark.django_db

# Five consecutive trading days, one of which a fixture file will omit.
FIVE_DAYS = [date(2024, 3, day) for day in (11, 12, 13, 14, 15)]


def rows_for(days, symbol="MSFT"):
    return [
        [symbol, day.isoformat(), "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "cov"]
        for day in days
    ]


# --------------------------------------------------------------------------- #
# The bitmap
# --------------------------------------------------------------------------- #


def test_membership_is_exact():
    bitmap = _ObservedDates()
    bitmap.add(date(2024, 3, 11))
    bitmap.add(date(2024, 3, 13))

    assert date(2024, 3, 11) in bitmap
    assert date(2024, 3, 13) in bitmap
    assert date(2024, 3, 12) not in bitmap
    assert len(bitmap) == 2
    assert bitmap.days() == {date(2024, 3, 11), date(2024, 3, 13)}


def test_a_date_before_the_first_one_grows_the_map_backwards():
    """The prepend path. A file is not guaranteed to be in date order."""
    bitmap = _ObservedDates()
    bitmap.add(date(2024, 3, 11))
    bitmap.add(date(2020, 1, 2))

    assert bitmap.days() == {date(2020, 1, 2), date(2024, 3, 11)}


def test_newest_first_input_is_handled():
    days = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(500)]

    bitmap = _ObservedDates()
    for day in reversed(days):  # worst case: every add prepends
        bitmap.add(day)

    assert bitmap.days() == set(days)


def test_a_decade_of_one_instruments_dates_costs_under_two_kilobytes():
    """The claim in the docstring, made testable.

    A ``set`` of 3653 ``date`` objects is roughly 200 KB. The bitmap is one bit
    per calendar day plus growth headroom.
    """
    days = [date(2015, 1, 1) + timedelta(days=offset) for offset in range(3653)]

    bitmap = _ObservedDates()
    for day in days:
        bitmap.add(day)

    assert len(bitmap) == len(days)
    assert bitmap.nbytes < 2048


# --------------------------------------------------------------------------- #
# The checkpoint payload
# --------------------------------------------------------------------------- #


def test_the_checkpoint_does_not_grow_with_the_number_of_rows(tmp_path):
    """The anti-regression test for checkpoint write amplification.

    A four-hundred-row chunked run writes forty checkpoints. If those carried the
    dates, they would grow linearly and the total written would be quadratic —
    which is what this asserts against, by bounding the size of *any* payload.
    """
    make_instrument("AAPL")
    user = make_user()
    days = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(400)]
    path = write_csv(tmp_path / "big.csv", rows_for(days, symbol="AAPL"))
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)

    sizes: list[int] = []

    def record_size(progress):
        sizes.append(len(json.dumps(progress.checkpoint)))

    result = run_ingestion(
        job,
        source_type=SourceType.CSV,
        path=path,
        policy=IngestionPolicy(chunk_size=10, commit_each_chunk=True),
        progress=record_size,
    )

    assert result.accepted == 400
    assert len(sizes) == 40, "one checkpoint per chunk"
    # With per-date data the final payload would be ~400 * 13 = 5200 characters.
    assert max(sizes) < 600, f"checkpoint payload grew: {sizes[:3]}...{sizes[-3:]}"
    assert max(sizes) - min(sizes) < 16, "only the digit count moves"


def test_the_checkpoint_carries_a_span_and_the_symbol_names(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    path = write_csv(tmp_path / "spanned.csv", rows_for(FIVE_DAYS, symbol="AAPL"))
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)

    run_ingestion(
        job,
        source_type=SourceType.CSV,
        path=path,
        policy=IngestionPolicy(chunk_size=2, commit_each_chunk=True),
    )

    job.refresh_from_db()
    coverage = job.checkpoint["coverage"]
    assert set(coverage) == {"first", "last", "symbols"}
    assert coverage["first"] == "2024-03-11"
    assert coverage["last"] == "2024-03-15"
    assert coverage["symbols"] == ["AAPL"]
    assert "observed" not in job.checkpoint


def test_a_run_of_any_size_holds_only_bits_per_day(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    days = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(300)]
    path = write_csv(tmp_path / "many.csv", rows_for(days, symbol="AAPL"))
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)

    run_ingestion(
        job,
        source_type=SourceType.CSV,
        path=path,
        policy=IngestionPolicy(chunk_size=25, commit_each_chunk=True),
    )

    job.refresh_from_db()
    # The span is 300 days: under 64 bytes of bitmap for the whole run.
    assert len(PriceRecord.objects.all()) == 300
    assert "coverage" in job.checkpoint


# --------------------------------------------------------------------------- #
# Restoring a checkpoint
# --------------------------------------------------------------------------- #


def test_restore_accepts_the_span_format():
    coverage = _Coverage.restore(
        {"first": "2024-03-11", "last": "2024-03-15", "symbols": ["MSFT"]}
    )

    assert coverage.first == date(2024, 3, 11)
    assert coverage.last == date(2024, 3, 15)
    assert coverage.symbols == {"MSFT"}


def test_restore_accepts_a_checkpoint_written_before_the_dates_were_dropped():
    """An older revision stored every date. Such a checkpoint still resumes."""
    coverage = _Coverage.restore(
        stored={"first": None, "last": None, "symbols": []},
        legacy_observed={"MSFT": ["2024-03-11", "2024-03-13"]},
    )

    assert coverage.first == date(2024, 3, 11)
    assert coverage.last == date(2024, 3, 13)
    assert coverage.observed("MSFT") == {date(2024, 3, 11), date(2024, 3, 13)}


def test_restore_from_nothing_is_empty():
    assert _Coverage.restore(None).empty


# --------------------------------------------------------------------------- #
# The gap check keeps its meaning across a resume
# --------------------------------------------------------------------------- #


def test_a_resumed_run_still_reports_a_day_it_never_saw(tmp_path):
    """The pre-crash dates come back from the rows the first attempt wrote.

    Without that, the resume would only know about the rows it read itself and
    would report the earlier days as missing — three gaps instead of one.
    """
    make_instrument("MSFT")
    add_calendar(*FIVE_DAYS)
    user = make_user()
    path = write_csv(
        tmp_path / "gap.csv",
        rows_for([date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 14), date(2024, 3, 15)]),
    )
    job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)
    policy = IngestionPolicy(chunk_size=2, commit_each_chunk=True)

    class KilledWorker(Exception):
        pass

    def die_after_the_first_chunk(progress):
        raise KilledWorker

    with pytest.raises(KilledWorker):
        run_ingestion(
            job, source_type=SourceType.CSV, path=path, policy=policy, progress=die_after_the_first_chunk
        )

    job.refresh_from_db()
    assert job.checkpoint["coverage"]["symbols"] == ["MSFT"]

    result = run_ingestion(
        job,
        source_type=SourceType.CSV,
        path=path,
        policy=policy,
        resume=job.checkpoint,
    )

    assert result.accepted == 4
    assert result.missing_trading_days == 1, "only 2024-03-13 is genuinely absent"
    assert PriceRecord.objects.count() == 4


def test_a_gap_is_judged_against_this_file_not_the_whole_table(tmp_path):
    """A day loaded by a *different* job does not make this file complete.

    The calendar rule is about the coverage of the batch being loaded. Reading
    "is there a row in the table" instead would silently hide a real gap whenever
    an earlier load happened to cover the same date.
    """
    make_instrument("MSFT")
    add_calendar(*FIVE_DAYS)
    user = make_user()

    # An earlier job loads the very day the next file is missing.
    ingest_file(
        user=user,
        path=write_csv(tmp_path / "earlier.csv", rows_for([date(2024, 3, 13)])),
    )

    result = ingest_file(
        user=user,
        path=write_csv(
            tmp_path / "later.csv",
            rows_for([date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 14), date(2024, 3, 15)]),
        ),
    )

    assert result.job.status == JobStatus.COMPLETED_WITH_ERRORS
    assert result.missing_trading_days == 1
