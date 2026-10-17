"""The data-quality rules, each a small independently testable function.

A rule is a pure function of one :class:`~apps.ingestion.etl.schemas.NormalizedRow`
and the batch :class:`RuleContext`. It returns a list of
:class:`~apps.ingestion.etl.schemas.Outcome` — empty when the rule held, one
entry per violation when it did not.

Two design decisions make the output readable rather than noisy:

* **Structural ownership.** A rule that needs a numeric high, low, open, and
  close stays silent when any of them is missing or non-positive, because the
  rule that owns that problem already reported it. An open of ``0`` is one
  finding (``non_positive_price``), not also a spurious "open outside range".
* **Ordering is data.** The duplicate rule registers each (symbol, date) it sees,
  so the *first* occurrence passes and every later one is a duplicate. That makes
  it the one rule with a side effect, which is documented on the function.

Because rules read only the normalised row, the same set applies to CSV, JSON,
XLSX, and API sources without a single branch on origin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .errors import RuleName
from .schemas import PRICE_FIELDS, NormalizedRow, Outcome


@dataclass
class RuleContext:
    """Batch-level facts a rule may need.

    ``known_symbols`` holds upper-cased tickers that resolve to an instrument;
    ``seen_keys`` accumulates (symbol, date) pairs for duplicate detection and is
    therefore mutated as rows stream past.
    """

    today: date = field(default_factory=date.today)
    known_symbols: set[str] = field(default_factory=set)
    seen_keys: set[tuple[str, str]] = field(default_factory=set)

    def is_known(self, symbol: str) -> bool:
        return symbol.strip().upper() in self.known_symbols


def _all_prices_present(row: NormalizedRow) -> bool:
    return all(price is not None for price in row.prices)


def _all_prices_positive(row: NormalizedRow) -> bool:
    return _all_prices_present(row) and all(price > 0 for price in row.prices)


def _range_is_meaningful(row: NormalizedRow) -> bool:
    """Range checks need a positive, correctly-ordered high and low."""
    return _all_prices_positive(row) and row.high >= row.low  # type: ignore[operator]


def rule_non_positive_price(row: NormalizedRow, context: RuleContext) -> list[Outcome]:
    """Every OHLC value must be strictly greater than zero.

    A zero is not a cheap asset; it is a missing or broken value.
    """
    if not _all_prices_present(row):
        return []
    return [
        Outcome(
            RuleName.NON_POSITIVE_PRICE,
            name,
            f"{name}={value} must be greater than zero",
        )
        for name, value in zip(PRICE_FIELDS, row.prices)
        if value is not None and value <= 0
    ]


def rule_high_below_low(row: NormalizedRow, context: RuleContext) -> list[Outcome]:
    """The day's high cannot be below its low."""
    if not _all_prices_positive(row):
        return []
    if row.high < row.low:  # type: ignore[operator]
        return [
            Outcome(
                RuleName.HIGH_BELOW_LOW,
                "high",
                f"high={row.high} is below low={row.low}",
            )
        ]
    return []


def rule_open_outside_range(row: NormalizedRow, context: RuleContext) -> list[Outcome]:
    """The open must sit inside the [low, high] range."""
    if not _range_is_meaningful(row):
        return []
    if row.open < row.low or row.open > row.high:  # type: ignore[operator]
        return [
            Outcome(
                RuleName.OPEN_OUTSIDE_RANGE,
                "open",
                f"open={row.open} is outside [{row.low}, {row.high}]",
            )
        ]
    return []


def rule_close_outside_range(row: NormalizedRow, context: RuleContext) -> list[Outcome]:
    """The close must sit inside the [low, high] range."""
    if not _range_is_meaningful(row):
        return []
    if row.close < row.low or row.close > row.high:  # type: ignore[operator]
        return [
            Outcome(
                RuleName.CLOSE_OUTSIDE_RANGE,
                "close",
                f"close={row.close} is outside [{row.low}, {row.high}]",
            )
        ]
    return []


def rule_negative_volume(row: NormalizedRow, context: RuleContext) -> list[Outcome]:
    """Traded volume cannot be negative. Zero is allowed: no trades is real."""
    if row.volume is None or row.volume >= 0:
        return []
    return [
        Outcome(RuleName.NEGATIVE_VOLUME, "volume", f"volume={row.volume} is negative")
    ]


def rule_future_date(row: NormalizedRow, context: RuleContext) -> list[Outcome]:
    """A trading day cannot be in the future.

    A future date usually means a clock skew or a mislabelled column, and it
    would poison any "as of today" metric.
    """
    if row.trading_date is None or row.trading_date <= context.today:
        return []
    return [
        Outcome(
            RuleName.FUTURE_DATE,
            "trading_date",
            f"{row.trading_date.isoformat()} is after today ({context.today.isoformat()})",
        )
    ]


def rule_duplicate_record(row: NormalizedRow, context: RuleContext) -> list[Outcome]:
    """Reject a second row for the same instrument and date in one batch.

    Side effect: the first time a (symbol, date) pair is seen it is registered in
    ``context.seen_keys``, so this rule is order dependent by nature. The first
    occurrence wins; a later one is the duplicate.
    """
    key = row.key
    if key is None:
        return []
    if key in context.seen_keys:
        return [
            Outcome(
                RuleName.DUPLICATE_RECORD,
                "",
                f"{key[0]} already has a row for {key[1]} in this batch",
            )
        ]
    context.seen_keys.add(key)
    return []


def rule_unknown_instrument(row: NormalizedRow, context: RuleContext) -> list[Outcome]:
    """The symbol must resolve to a known instrument before anything is stored."""
    if row.symbol is None or context.is_known(row.symbol):
        return []
    return [
        Outcome(
            RuleName.UNKNOWN_INSTRUMENT,
            "symbol",
            f"symbol {row.symbol!r} matches no known instrument",
        )
    ]


# Applied in this order. Order matters only for the stateful duplicate rule,
# which must see rows in file order.
DOMAIN_RULES = (
    rule_non_positive_price,
    rule_high_below_low,
    rule_open_outside_range,
    rule_close_outside_range,
    rule_negative_volume,
    rule_future_date,
    rule_duplicate_record,
    rule_unknown_instrument,
)


def missing_trading_dates(
    *,
    symbol: str,
    expected_dates: set[date],
    observed_dates: set[date],
) -> list[Outcome]:
    """Report expected trading days for which no price row was supplied.

    This is the one rule that is an *absence* rather than a bad value, so it is
    evaluated over a whole batch after loading, not per row. It has no source row
    to point at, so its outcomes carry ``row_number = 0`` and name the missing
    date in the detail instead.

    Contract
    --------
    in : one symbol, the dates the calendar says were trading days, and the dates
         that actually arrived for that symbol.
    out: one ``missing_trading_date`` outcome per absent date.
    """
    return [
        Outcome(
            RuleName.MISSING_TRADING_DATE,
            "trading_date",
            f"{symbol} has no price for expected trading day {missing.isoformat()}",
        )
        for missing in sorted(expected_dates - observed_dates)
    ]
