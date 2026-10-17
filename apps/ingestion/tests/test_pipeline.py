"""End-to-end pipeline runs: clean, partial, malformed, and every policy.

These are the tests that would catch an integration mistake no unit test can: a
row that validates but is not stored, a count that does not reconcile, a bad file
that leaves half a batch behind.
"""

import json
from collections import Counter
from datetime import date

import pytest

from apps.ingestion.etl.errors import BatchError, RuleName
from apps.ingestion.etl.pipeline import IngestionPolicy
from apps.ingestion.models import JobStatus
from apps.ingestion.services import ingest_file
from apps.marketdata.models import PriceRecord

from .helpers import (
    SAMPLE_DIR,
    add_calendar,
    make_instrument,
    make_user,
    seed_sample_dataset,
    write_csv,
    write_json,
    write_xlsx,
)

# The exact dirty batch from the generator, one row per rule plus two good rows.
DIRTY_ROWS = [
    ["AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "synthetic_dirty"],
    ["", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "synthetic_dirty"],
    ["MSFT", "2024-03-12", "", "102.00", "99.00", "101.00", "101.00", 1000, "synthetic_dirty"],
    ["MSFT", "2024-03-13", "100.00", "102.00", "", "101.00", "101.00", 1000, "synthetic_dirty"],
    ["SPY", "2024-03-12", "0.00", "102.00", "99.00", "101.00", "101.00", 1000, "synthetic_dirty"],
    ["SPY", "2024-03-13", "100.00", "102.00", "99.00", "-5.00", "101.00", 1000, "synthetic_dirty"],
    ["GOOG", "2024-03-12", "100.00", "98.00", "99.00", "101.00", "101.00", 1000, "synthetic_dirty"],
    ["AMZN", "2024-03-12", "105.00", "102.00", "99.00", "101.00", "101.00", 1000, "synthetic_dirty"],
    ["AMZN", "2024-03-13", "100.00", "102.00", "99.00", "105.00", "101.00", 1000, "synthetic_dirty"],
    ["TSLA", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", -100, "synthetic_dirty"],
    ["NVDA", "2024-13-45", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "synthetic_dirty"],
    ["NVDA", "2099-01-01", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "synthetic_dirty"],
    ["AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "synthetic_dirty"],
    ["ZZZZ", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "synthetic_dirty"],
    ["MSFT", "2024-03-14", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "synthetic_dirty"],
]

DIRTY_INSTRUMENTS = ("AAPL", "MSFT", "SPY", "GOOG", "AMZN", "TSLA", "NVDA")

EXPECTED_DIRTY_FAILURES = {
    RuleName.MISSING_SYMBOL: 1,
    RuleName.MISSING_REQUIRED_FIELD: 2,
    RuleName.NON_POSITIVE_PRICE: 2,
    RuleName.HIGH_BELOW_LOW: 1,
    RuleName.OPEN_OUTSIDE_RANGE: 1,
    RuleName.CLOSE_OUTSIDE_RANGE: 1,
    RuleName.NEGATIVE_VOLUME: 1,
    RuleName.INVALID_DATE: 1,
    RuleName.FUTURE_DATE: 1,
    RuleName.DUPLICATE_RECORD: 1,
    RuleName.UNKNOWN_INSTRUMENT: 1,
}


def seed_dirty_instruments():
    for symbol in DIRTY_INSTRUMENTS:
        make_instrument(symbol)


# --------------------------------------------------------------------------- #
# Clean load
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_a_clean_csv_loads_and_succeeds(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    path = write_csv(
        tmp_path / "clean.csv",
        [
            ["AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "clean"],
            ["AAPL", "2024-03-13", "101.00", "103.00", "100.00", "102.00", "102.00", 1100, "clean"],
        ],
    )

    result = ingest_file(user=user, path=path)

    job = result.job
    assert job.status == JobStatus.COMPLETED
    assert (job.rows_received, job.rows_accepted, job.rows_rejected) == (2, 2, 0)
    assert PriceRecord.objects.count() == 2
    assert job.rule_summaries.count() == len(RuleName)
    assert job.validation_errors.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("suffix", ["csv", "json", "xlsx"])
def test_the_same_rows_load_from_any_format(tmp_path, suffix):
    make_instrument("AAPL")
    user = make_user()
    header = ["symbol", "trading_date", "open", "high", "low", "close", "adjusted_close", "volume", "source"]
    values = ["AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "clean"]

    if suffix == "csv":
        path = write_csv(tmp_path / f"clean.{suffix}", [values])
    elif suffix == "json":
        path = write_json(tmp_path / f"clean.{suffix}", [dict(zip(header, values))])
    else:
        path = write_xlsx(tmp_path / f"clean.{suffix}", [values])

    result = ingest_file(user=user, path=path)

    assert result.job.status == JobStatus.COMPLETED
    assert PriceRecord.objects.count() == 1


# --------------------------------------------------------------------------- #
# Partial load (the dirty fixture)
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_the_dirty_batch_is_partial_with_one_finding_per_bad_row(tmp_path):
    seed_dirty_instruments()
    user = make_user()
    path = write_csv(tmp_path / "dirty.csv", DIRTY_ROWS)
    policy = IngestionPolicy(run_calendar_check=False)

    result = ingest_file(user=user, path=path, policy=policy)

    job = result.job
    assert job.status == JobStatus.COMPLETED_WITH_ERRORS
    assert (job.rows_received, job.rows_accepted, job.rows_rejected) == (15, 2, 13)
    assert PriceRecord.objects.count() == 2

    counts = Counter(job.validation_errors.values_list("error_code", flat=True))
    assert counts == Counter({str(rule): n for rule, n in EXPECTED_DIRTY_FAILURES.items()})
    assert job.validation_errors.count() == 13


@pytest.mark.django_db
def test_the_dirty_batch_records_a_rule_summary_per_rule(tmp_path):
    seed_dirty_instruments()
    user = make_user()
    path = write_csv(tmp_path / "dirty.csv", DIRTY_ROWS)
    policy = IngestionPolicy(run_calendar_check=False)

    job = ingest_file(user=user, path=path, policy=policy).job

    summaries = {summary.rule_name: summary for summary in job.rule_summaries.all()}
    assert summaries[RuleName.MISSING_SYMBOL].failed == 1
    assert summaries[RuleName.MISSING_SYMBOL].passed == 14
    assert summaries[RuleName.DUPLICATE_RECORD].failed == 1
    assert summaries[RuleName.MISSING_SYMBOL].status == "fail"


# --------------------------------------------------------------------------- #
# Abort policy
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_the_abort_policy_rolls_the_whole_batch_back(tmp_path):
    seed_dirty_instruments()
    user = make_user()
    path = write_csv(tmp_path / "dirty.csv", DIRTY_ROWS)
    policy = IngestionPolicy(on_invalid="abort", run_calendar_check=False)

    job = ingest_file(user=user, path=path, policy=policy).job

    assert job.status == JobStatus.FAILED
    assert PriceRecord.objects.count() == 0
    codes = set(job.validation_errors.values_list("error_code", flat=True))
    assert BatchError.ABORTED_ON_INVALID in codes


# --------------------------------------------------------------------------- #
# Structural failures
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_a_missing_required_column_fails_before_any_row_is_read(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    header = ["symbol", "trading_date", "open", "high", "low", "close", "adjusted_close", "source"]
    path = write_csv(tmp_path / "narrow.csv", [["AAPL", "2024-03-12", "1", "2", "0.5", "1.5", "1.5", "x"]], header=header)

    job = ingest_file(user=user, path=path).job

    assert job.status == JobStatus.FAILED
    assert PriceRecord.objects.count() == 0
    error = job.validation_errors.get()
    assert error.error_code == BatchError.MISSING_COLUMN
    assert error.field_name == "volume"


@pytest.mark.django_db
def test_a_truncated_json_file_fails_the_job_and_rolls_back(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    record = {
        "symbol": "AAPL",
        "trading_date": "2024-03-12",
        "open": "100.00",
        "high": "102.00",
        "low": "99.00",
        "close": "101.00",
        "adjusted_close": "101.00",
        "volume": 1000,
        "source": "clean",
    }
    # A valid first record, then the array is never closed: the schema check
    # passes, the first row validates, and the truncation is only found mid-stream.
    path = tmp_path / "broken.json"
    path.write_text("[" + json.dumps(record), encoding="utf-8")

    job = ingest_file(user=user, path=path).job

    assert job.status == JobStatus.FAILED
    assert PriceRecord.objects.count() == 0
    assert job.validation_errors.get().error_code == BatchError.MALFORMED_SOURCE


@pytest.mark.django_db
def test_an_empty_source_fails_with_an_empty_source_error(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    path = write_csv(tmp_path / "empty.csv", [])

    job = ingest_file(user=user, path=path).job

    assert job.status == JobStatus.FAILED
    assert BatchError.EMPTY_SOURCE in set(job.validation_errors.values_list("error_code", flat=True))


# --------------------------------------------------------------------------- #
# Duplicate handling across files
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_re_ingesting_the_same_file_updates_rather_than_duplicating(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    path = write_csv(
        tmp_path / "clean.csv",
        [["AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "clean"]],
    )

    ingest_file(user=user, path=path)
    result = ingest_file(user=user, path=path)

    assert PriceRecord.objects.count() == 1
    assert result.updated == 1
    assert result.job.status == JobStatus.COMPLETED


@pytest.mark.django_db
def test_the_fail_policy_reports_a_cross_file_duplicate(tmp_path):
    make_instrument("AAPL")
    user = make_user()
    path = write_csv(
        tmp_path / "clean.csv",
        [["AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "clean"]],
    )

    ingest_file(user=user, path=path)
    policy = IngestionPolicy(on_existing="fail", run_calendar_check=False)
    job = ingest_file(user=user, path=path, policy=policy).job

    assert PriceRecord.objects.count() == 1
    assert RuleName.DUPLICATE_RECORD in set(job.validation_errors.values_list("error_code", flat=True))


# --------------------------------------------------------------------------- #
# Missing trading day
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_a_missing_trading_day_is_reported(tmp_path):
    make_instrument("MSFT")
    add_calendar(date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 13))
    user = make_user()
    path = write_csv(
        tmp_path / "gap.csv",
        [
            ["MSFT", "2024-03-11", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "clean"],
            ["MSFT", "2024-03-13", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "clean"],
        ],
    )

    result = ingest_file(user=user, path=path)

    assert result.job.status == JobStatus.COMPLETED_WITH_ERRORS
    assert result.missing_trading_days == 1
    error = job_missing_date_error(result.job)
    assert "2024-03-12" in error.detail


def job_missing_date_error(job):
    return job.validation_errors.get(error_code=RuleName.MISSING_TRADING_DATE)


# --------------------------------------------------------------------------- #
# The committed sample dataset
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_the_sample_dataset_loads_end_to_end():
    seed_sample_dataset()
    user = make_user()

    batches = sorted((SAMPLE_DIR / "batches").iterdir())
    assert len(batches) == 6
    for batch in batches:
        job = ingest_file(user=user, path=batch).job
        assert job.status == JobStatus.COMPLETED, job.error_summary

    assert PriceRecord.objects.count() == 360

    dirty_job = ingest_file(
        user=user,
        path=SAMPLE_DIR / "dirty" / "prices_dirty.csv",
        policy=IngestionPolicy(run_calendar_check=False),
    ).job
    assert (dirty_job.rows_received, dirty_job.rows_accepted, dirty_job.rows_rejected) == (15, 2, 13)

    gap_result = ingest_file(user=user, path=SAMPLE_DIR / "dirty" / "prices_missing_dates.csv")
    assert gap_result.missing_trading_days == 1
