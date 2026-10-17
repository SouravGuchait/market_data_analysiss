"""Normalisation: alias resolution, type coercion, and structural issues.

Normalisation is where a vendor's spelling becomes our schema, so these tests
pin the two things that matter: the alias map actually resolves real-world
headers, and a bad value becomes a recorded issue rather than an exception.
"""

from datetime import date
from decimal import Decimal

from apps.ingestion.etl.errors import RuleName
from apps.ingestion.etl.schemas import (
    missing_required_columns,
    normalize_row,
    resolve_columns,
    sanitize_raw,
)

CANONICAL_HEADER = [
    "symbol",
    "trading_date",
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
    "source",
]


def raw_row(**overrides):
    values = {
        "symbol": "AAPL",
        "trading_date": "2024-03-12",
        "open": "100.00",
        "high": "102.00",
        "low": "99.00",
        "close": "101.00",
        "adjusted_close": "101.00",
        "volume": "1000",
        "source": "test",
    }
    values.update(overrides)
    return values


def test_vendor_aliases_resolve_to_canonical_columns():
    columns = resolve_columns(
        ["Ticker", "Date", "Open Price", "High", "Low", "Adj Close", "Vol"]
    )

    assert columns["symbol"] == "Ticker"
    assert columns["trading_date"] == "Date"
    assert columns["open"] == "Open Price"
    assert columns["adjusted_close"] == "Adj Close"
    assert columns["volume"] == "Vol"


def test_alias_matching_ignores_case_and_punctuation():
    columns = resolve_columns(["TICKER", "trading-date", "OPEN_PRICE", "VOLUME_TRADED"])

    assert columns["symbol"] == "TICKER"
    assert columns["trading_date"] == "trading-date"
    assert columns["open"] == "OPEN_PRICE"
    assert columns["volume"] == "VOLUME_TRADED"


def test_missing_required_columns_lists_only_absent_required_fields():
    columns = resolve_columns(["symbol", "trading_date", "open", "high", "low", "close"])

    assert missing_required_columns(columns) == ["volume"]


def test_a_complete_header_has_nothing_missing():
    assert missing_required_columns(resolve_columns(CANONICAL_HEADER)) == []


def test_a_clean_row_becomes_typed_values():
    row = normalize_row(
        row_number=1,
        raw=raw_row(),
        columns=resolve_columns(CANONICAL_HEADER),
        default_source="fallback",
    )

    assert row.symbol == "AAPL"
    assert row.trading_date == date(2024, 3, 12)
    assert row.open == Decimal("100.00")
    assert row.volume == 1000
    assert row.source == "test"
    assert row.is_clean


def test_a_blank_symbol_is_recorded_not_raised():
    row = normalize_row(
        row_number=2,
        raw=raw_row(symbol="  "),
        columns=resolve_columns(CANONICAL_HEADER),
    )

    assert row.symbol is None
    assert [issue.rule for issue in row.issues] == [RuleName.MISSING_SYMBOL]


def test_an_unparseable_date_is_recorded_as_invalid_date():
    row = normalize_row(
        row_number=3,
        raw=raw_row(trading_date="2024-13-45"),
        columns=resolve_columns(CANONICAL_HEADER),
    )

    assert row.trading_date is None
    assert RuleName.INVALID_DATE in {issue.rule for issue in row.issues}


def test_a_non_numeric_price_is_recorded_as_a_missing_field():
    row = normalize_row(
        row_number=4,
        raw=raw_row(open="one hundred"),
        columns=resolve_columns(CANONICAL_HEADER),
    )

    assert row.open is None
    assert RuleName.MISSING_REQUIRED_FIELD in {issue.rule for issue in row.issues}


def test_a_fractional_volume_is_rejected_not_truncated():
    row = normalize_row(
        row_number=5,
        raw=raw_row(volume="1000.5"),
        columns=resolve_columns(CANONICAL_HEADER),
    )

    assert row.volume is None
    assert RuleName.MISSING_REQUIRED_FIELD in {issue.rule for issue in row.issues}


def test_optional_columns_may_be_absent():
    header = [name for name in CANONICAL_HEADER if name not in {"adjusted_close", "source"}]
    row = normalize_row(
        row_number=6,
        raw={
            "symbol": "AAPL",
            "trading_date": "2024-03-12",
            "open": "100",
            "high": "102",
            "low": "99",
            "close": "101",
            "volume": "1000",
        },
        columns=resolve_columns(header),
        default_source="csv:batch.csv",
    )

    assert row.adjusted_close is None
    assert row.source == "csv:batch.csv"
    assert row.is_clean


def test_a_float_price_does_not_carry_binary_rounding_error():
    row = normalize_row(
        row_number=7,
        raw=raw_row(open=0.1),
        columns=resolve_columns(CANONICAL_HEADER),
    )

    # Decimal(0.1) would be 0.1000000000000000055511151231257827..., which is not money.
    assert row.open == Decimal("0.1")


def test_key_is_an_upper_cased_symbol_and_iso_date():
    row = normalize_row(
        row_number=8,
        raw=raw_row(symbol="aapl"),
        columns=resolve_columns(CANONICAL_HEADER),
    )

    assert row.key == ("AAPL", "2024-03-12")


def test_key_is_none_without_both_symbol_and_date():
    row = normalize_row(
        row_number=9,
        raw=raw_row(trading_date="2024-13-45"),
        columns=resolve_columns(CANONICAL_HEADER),
    )

    assert row.key is None


def test_sanitize_raw_bounds_size_and_makes_values_json_safe():
    cleaned = sanitize_raw({"symbol": "AAPL", "blob": "x" * 5000})

    assert set(cleaned) == {"_truncated"}
    assert len(cleaned["_truncated"]) == 2000


def test_sanitize_raw_keeps_small_rows_intact():
    assert sanitize_raw({"symbol": "AAPL", "volume": 1000}) == {
        "symbol": "AAPL",
        "volume": 1000,
    }
