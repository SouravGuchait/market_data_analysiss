"""The pandas transformation: typing, ordering, and de-duplication."""

from datetime import date
from decimal import Decimal

from apps.ingestion.etl.transform import frame_to_records, prepare_for_load, to_frame

from .helpers import make_row


def test_to_frame_keeps_prices_as_decimal_and_upper_cases_the_symbol():
    frame = to_frame([make_row(symbol="aapl")])

    assert list(frame.columns) == [
        "symbol",
        "trading_date",
        "open",
        "high",
        "low",
        "close",
        "adjusted_close",
        "volume",
        "source",
        "row_number",
    ]
    assert frame.loc[0, "symbol"] == "AAPL"
    assert frame.loc[0, "open"] == Decimal("100.00")


def test_to_frame_quantizes_prices_onto_the_model_grid():
    frame = to_frame([make_row(open=Decimal("100.123456789"))])

    assert frame.loc[0, "open"] == Decimal("100.12345679")


def test_to_frame_of_nothing_still_has_the_columns():
    frame = to_frame([])

    assert frame.empty
    assert "symbol" in frame.columns


def test_prepare_for_load_sorts_by_symbol_then_date():
    rows = [
        make_row(row_number=1, symbol="MSFT", trading_date=date(2024, 3, 12)),
        make_row(row_number=2, symbol="AAPL", trading_date=date(2024, 3, 13)),
        make_row(row_number=3, symbol="AAPL", trading_date=date(2024, 3, 11)),
    ]

    prepared = prepare_for_load(to_frame(rows))

    assert list(zip(prepared["symbol"], prepared["trading_date"])) == [
        ("AAPL", date(2024, 3, 11)),
        ("AAPL", date(2024, 3, 13)),
        ("MSFT", date(2024, 3, 12)),
    ]


def test_prepare_for_load_keeps_the_last_of_a_repeated_key():
    rows = [
        make_row(row_number=1, close=Decimal("101.00")),
        make_row(row_number=2, close=Decimal("102.00")),
    ]

    prepared = prepare_for_load(to_frame(rows))

    assert len(prepared) == 1
    assert prepared.loc[0, "close"] == Decimal("102.00")
    assert prepared.loc[0, "row_number"] == 2


def test_prepare_for_load_fills_a_blank_source():
    prepared = prepare_for_load(to_frame([make_row(source="")]))

    assert prepared.loc[0, "source"] == "unknown"


def test_prepare_for_load_casts_volume_to_a_plain_int():
    prepared = prepare_for_load(to_frame([make_row(volume=1000)]))

    assert prepared.loc[0, "volume"] == 1000
    assert isinstance(prepared.loc[0, "volume"], int)


def test_frame_to_records_returns_plain_dicts():
    records = frame_to_records(prepare_for_load(to_frame([make_row()])))

    assert isinstance(records[0], dict)
    assert records[0]["symbol"] == "AAPL"
