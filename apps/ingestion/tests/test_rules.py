"""Each data-quality rule, in isolation, plus the precedence between them.

The precedence tests are the important ones: they assert that a single broken
field produces a single finding, so an operator reading the error table is not
misled by a cascade of consequences.
"""

from datetime import date
from decimal import Decimal

from apps.ingestion.etl.errors import RuleName
from apps.ingestion.etl.rules import (
    RuleContext,
    missing_trading_dates,
    rule_close_outside_range,
    rule_duplicate_record,
    rule_future_date,
    rule_high_below_low,
    rule_negative_volume,
    rule_non_positive_price,
    rule_open_outside_range,
    rule_unknown_instrument,
)

from .helpers import make_row


def context(**overrides):
    values = {"today": date(2024, 3, 20), "known_symbols": {"AAPL"}}
    values.update(overrides)
    return RuleContext(**values)


# --------------------------------------------------------------------------- #
# non_positive_price
# --------------------------------------------------------------------------- #

def test_non_positive_price_flags_every_non_positive_ohlc_value():
    row = make_row(open=Decimal(0), close=Decimal(-5))

    outcomes = rule_non_positive_price(row, context())

    assert {outcome.field for outcome in outcomes} == {"open", "close"}
    assert all(outcome.rule == RuleName.NON_POSITIVE_PRICE for outcome in outcomes)


def test_non_positive_price_is_silent_when_a_price_is_missing():
    assert rule_non_positive_price(make_row(open=None), context()) == []


def test_a_clean_row_satisfies_non_positive_price():
    assert rule_non_positive_price(make_row(), context()) == []


# --------------------------------------------------------------------------- #
# high_below_low and range checks
# --------------------------------------------------------------------------- #

def test_high_below_low_fires_when_high_is_below_low():
    outcomes = rule_high_below_low(make_row(high=Decimal(98), low=Decimal(99)), context())

    assert [outcome.rule for outcome in outcomes] == [RuleName.HIGH_BELOW_LOW]


def test_high_below_low_defers_to_non_positive_price():
    row = make_row(open=Decimal(0), high=Decimal(98), low=Decimal(99))

    assert rule_high_below_low(row, context()) == []


def test_open_outside_range_fires_when_open_breaks_the_high():
    outcomes = rule_open_outside_range(make_row(open=Decimal(105)), context())

    assert [outcome.rule for outcome in outcomes] == [RuleName.OPEN_OUTSIDE_RANGE]


def test_close_outside_range_fires_when_close_breaks_the_low():
    outcomes = rule_close_outside_range(make_row(close=Decimal(98)), context())

    assert [outcome.rule for outcome in outcomes] == [RuleName.CLOSE_OUTSIDE_RANGE]


def test_range_checks_defer_when_the_range_itself_is_broken():
    row = make_row(open=Decimal(105), high=Decimal(98), low=Decimal(99))

    # The high/low ordering is the owner of the problem; range checks stay quiet.
    assert rule_open_outside_range(row, context()) == []
    assert rule_close_outside_range(row, context()) == []


def test_range_endpoints_are_allowed():
    row = make_row(open=Decimal(99), close=Decimal(102))

    assert rule_open_outside_range(row, context()) == []
    assert rule_close_outside_range(row, context()) == []


# --------------------------------------------------------------------------- #
# negative_volume
# --------------------------------------------------------------------------- #

def test_negative_volume_fires_below_zero():
    outcomes = rule_negative_volume(make_row(volume=-1), context())

    assert [outcome.rule for outcome in outcomes] == [RuleName.NEGATIVE_VOLUME]


def test_zero_volume_is_allowed():
    assert rule_negative_volume(make_row(volume=0), context()) == []


# --------------------------------------------------------------------------- #
# future_date
# --------------------------------------------------------------------------- #

def test_future_date_fires_after_today():
    outcomes = rule_future_date(make_row(trading_date=date(2099, 1, 1)), context())

    assert [outcome.rule for outcome in outcomes] == [RuleName.FUTURE_DATE]


def test_today_is_not_a_future_date():
    assert rule_future_date(make_row(trading_date=date(2024, 3, 20)), context()) == []


# --------------------------------------------------------------------------- #
# duplicate_record
# --------------------------------------------------------------------------- #

def test_duplicate_record_passes_the_first_and_flags_the_second():
    ctx = context()
    first = make_row(row_number=1)
    second = make_row(row_number=2)

    assert rule_duplicate_record(first, ctx) == []
    outcomes = rule_duplicate_record(second, ctx)

    assert [outcome.rule for outcome in outcomes] == [RuleName.DUPLICATE_RECORD]
    assert outcomes[0].field == ""


def test_duplicate_record_ignores_rows_without_a_full_key():
    ctx = context()

    assert rule_duplicate_record(make_row(symbol=None), ctx) == []
    assert rule_duplicate_record(make_row(trading_date=None), ctx) == []


# --------------------------------------------------------------------------- #
# unknown_instrument
# --------------------------------------------------------------------------- #

def test_unknown_instrument_fires_for_an_unregistered_symbol():
    outcomes = rule_unknown_instrument(make_row(symbol="ZZZZ"), context())

    assert [outcome.rule for outcome in outcomes] == [RuleName.UNKNOWN_INSTRUMENT]


def test_a_known_symbol_passes_unknown_instrument():
    assert rule_unknown_instrument(make_row(symbol="aapl"), context()) == []


def test_unknown_instrument_defers_when_the_symbol_is_missing():
    assert rule_unknown_instrument(make_row(symbol=None), context()) == []


# --------------------------------------------------------------------------- #
# missing_trading_dates
# --------------------------------------------------------------------------- #

def test_missing_trading_dates_reports_the_absent_days_in_order():
    outcomes = missing_trading_dates(
        symbol="MSFT",
        expected_dates={date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 13)},
        observed_dates={date(2024, 3, 11), date(2024, 3, 13)},
    )

    assert len(outcomes) == 1
    assert outcomes[0].rule == RuleName.MISSING_TRADING_DATE
    assert "2024-03-12" in outcomes[0].detail


def test_missing_trading_dates_is_empty_when_nothing_is_missing():
    days = {date(2024, 3, 11), date(2024, 3, 12)}

    assert missing_trading_dates(symbol="MSFT", expected_dates=days, observed_dates=days) == []
