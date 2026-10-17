"""Persistence: the upsert, and the explicit answer to a repeated (instrument, date).

The three ``on_existing`` policies are the heart of this file. Duplicate handling
is a design decision, not an accident of whether an insert happens to collide, so
each policy is asserted directly.
"""

from datetime import date
from decimal import Decimal

import pytest

from apps.ingestion.etl.persistence import (
    SymbolIndex,
    existing_price_keys,
    expected_calendar_dates,
    upsert_kwargs,
    upsert_prices,
)
from apps.ingestion.models import IngestionJob, SourceType
from apps.marketdata.models import PriceRecord, TradingCalendar

from .helpers import make_instrument, make_user


def make_job(user):
    return IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)


def record(**overrides):
    values = {
        "symbol": "AAPL",
        "trading_date": date(2024, 3, 12),
        "open": Decimal("100.00"),
        "high": Decimal("102.00"),
        "low": Decimal("99.00"),
        "close": Decimal("101.00"),
        "adjusted_close": Decimal("101.00"),
        "volume": 1000,
        "source": "test",
    }
    values.update(overrides)
    return values


@pytest.mark.django_db
def test_symbol_index_is_case_insensitive():
    make_instrument("AAPL")

    index = SymbolIndex.load()

    assert index.id_for("aapl") == index.id_for("AAPL")
    assert "AAPL" in index.known_symbols
    assert index.id_for("ZZZZ") is None


@pytest.mark.django_db
def test_upsert_creates_rows():
    make_instrument("AAPL")
    job = make_job(make_user())
    index = SymbolIndex.load()

    result = upsert_prices(job=job, records=[record()], symbols=index)

    assert (result.created, result.updated, result.skipped) == (1, 0, 0)
    stored = PriceRecord.objects.get()
    assert stored.ingestion_job_id == job.pk
    assert stored.close == Decimal("101.00")


@pytest.mark.django_db
def test_update_policy_refreshes_an_existing_row_without_duplicating_it():
    make_instrument("AAPL")
    first_job = make_job(make_user("first@example.test"))
    second_job = make_job(make_user("second@example.test"))
    index = SymbolIndex.load()

    upsert_prices(job=first_job, records=[record(close=Decimal("101.00"))], symbols=index)
    result = upsert_prices(
        job=second_job, records=[record(close=Decimal("222.00"))], symbols=index
    )

    assert (result.created, result.updated) == (0, 1)
    assert PriceRecord.objects.count() == 1
    stored = PriceRecord.objects.get()
    assert stored.close == Decimal("222.00")
    assert stored.ingestion_job_id == second_job.pk


@pytest.mark.django_db
def test_skip_policy_leaves_an_existing_row_untouched():
    make_instrument("AAPL")
    first_job = make_job(make_user("first@example.test"))
    second_job = make_job(make_user("second@example.test"))
    index = SymbolIndex.load()

    upsert_prices(job=first_job, records=[record(close=Decimal("101.00"))], symbols=index)
    result = upsert_prices(
        job=second_job,
        records=[record(close=Decimal("222.00"))],
        symbols=index,
        on_existing="skip",
    )

    assert (result.created, result.updated, result.skipped) == (0, 0, 1)
    assert PriceRecord.objects.get().close == Decimal("101.00")


@pytest.mark.django_db
def test_fail_policy_reports_the_duplicate_and_writes_nothing():
    make_instrument("AAPL")
    job = make_job(make_user())
    index = SymbolIndex.load()

    upsert_prices(job=job, records=[record()], symbols=index)
    result = upsert_prices(
        job=job, records=[record()], symbols=index, on_existing="fail"
    )

    assert result.duplicates == [("AAPL", "2024-03-12")]
    assert (result.created, result.updated, result.skipped) == (0, 0, 0)
    assert PriceRecord.objects.count() == 1


@pytest.mark.django_db
def test_an_unknown_on_existing_policy_is_rejected():
    make_instrument("AAPL")
    job = make_job(make_user())

    with pytest.raises(ValueError):
        upsert_prices(
            job=job, records=[record()], symbols=SymbolIndex.load(), on_existing="merge"
        )


@pytest.mark.django_db
def test_existing_price_keys_finds_stored_pairs():
    instrument = make_instrument("AAPL")
    job = make_job(make_user())
    upsert_prices(job=job, records=[record()], symbols=SymbolIndex.load())

    keys = existing_price_keys([instrument.pk], [date(2024, 3, 12), date(2024, 3, 13)])

    assert keys == {(instrument.pk, date(2024, 3, 12))}


def test_upsert_kwargs_require_a_conflict_target_on_sqlite_and_postgres():
    kwargs = upsert_kwargs(supports_conflict_target=True)

    assert kwargs["unique_fields"] == ["instrument", "trading_date"]
    assert kwargs["update_conflicts"] is True
    assert "ingestion_job" in kwargs["update_fields"]


def test_upsert_kwargs_omit_the_target_on_mysql_style_backends():
    # MySQL's ON DUPLICATE KEY UPDATE takes no target; passing one raises there
    # but not on SQLite, so the choice is asserted directly.
    kwargs = upsert_kwargs(supports_conflict_target=False)

    assert "unique_fields" not in kwargs
    assert kwargs["update_conflicts"] is True


@pytest.mark.django_db
def test_expected_calendar_dates_is_inclusive_of_both_ends():
    TradingCalendar.objects.bulk_create(
        [
            TradingCalendar(date=date(2024, 3, 11)),
            TradingCalendar(date=date(2024, 3, 12)),
            TradingCalendar(date=date(2024, 3, 13)),
        ]
    )

    found = expected_calendar_dates(date(2024, 3, 11), date(2024, 3, 12))

    assert found == {date(2024, 3, 11), date(2024, 3, 12)}
