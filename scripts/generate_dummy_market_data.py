"""Generate synthetic market-data fixtures that match this project's models.

Why this file exists
--------------------
The ingestion engine needs inputs that behave like real feeds and that exercise
every data-quality rule, without shipping licensed vendor data. This script
writes CSV, JSON, and XLSX files for every table in the schema, plus
deliberately broken price batches, and records the database rows each file
should produce so tests can assert exact counts instead of eyeballing output.

It imports no Django, so it runs before migrations and inside CI with no
database. Every value is synthetic and unsuitable for any real decision.

Input / output contract
-----------------------
Input : command-line flags (see :func:`parse_args`).
Output: a directory tree under ``--output-dir``:

    reference/<table>.<ext>            one file per reference table, 3 formats
    batches/prices_batch_NNN.<ext>     clean price batches, format cycles
    dirty/prices_dirty.<ext>           one row per violated rule
    dirty/prices_missing_dates.csv     a deliberate calendar gap
    manifest.json                      what was generated, and with which seed
    EXPECTED_RESULTS.md                rows the ORM should end up holding

Columns are named after the model fields they load, with two deliberate
translations, because a file cannot hold a foreign key:

    Instrument.exchange       -> exchange_code   (matches Exchange.code)
    IngestionJob.created_by   -> created_by_email (matches User.email)
    IngestionFile.job         -> job_id           (matches IngestionJob.id)
    IngestionValidationError.job -> job_id
    PriceRecord.instrument    -> symbol           (matches Instrument.symbol)

Examples
--------

Generate synthetic market-data fixtures that match this project's models.

Why this file exists
--------------------
The ingestion engine needs inputs that behave like real feeds and that exercise
every data-quality rule, without shipping licensed vendor data. This script
writes CSV, JSON, and XLSX files for every table in the schema, plus
deliberately broken price batches, and records the database rows each file
should produce so tests can assert exact counts instead of eyeballing output.

It imports no Django, so it runs before migrations and inside CI with no
database. Every value is synthetic and unsuitable for any real decision.

Input / output contract
-----------------------
Input : command-line flags (see :func:`parse_args`).
Output: a directory tree under ``--output-dir``:

    reference/<table>.<ext>            one file per reference table, 3 formats
    batches/prices_batch_NNN.<ext>     clean price batches, format cycles
    dirty/prices_dirty.<ext>           one row per violated rule
    dirty/prices_missing_dates.csv     a deliberate calendar gap
    manifest.json                      what was generated, and with which seed
    EXPECTED_RESULTS.md                rows the ORM should end up holding

Columns are named after the model fields they load, with two deliberate
translations, because a file cannot hold a foreign key:

    Instrument.exchange       -> exchange_code   (matches Exchange.code)
    IngestionJob.created_by   -> created_by_email (matches User.email)
    IngestionFile.job         -> job_id           (matches IngestionJob.id)
    IngestionValidationError.job -> job_id
    PriceRecord.instrument    -> symbol           (matches Instrument.symbol)

Examples
--------
    python scripts/generate_dummy_market_data.py
    python scripts/generate_dummy_market_data.py --instruments 200 --trading-days 750
    python scripts/generate_dummy_market_data.py --output-dir outputs/etl_dev --seed 7
    python scripts/generate_dummy_market_data.py --instruments 200 --trading-days 750
    python scripts/generate_dummy_market_data.py --output-dir outputs/etl_dev --seed 7
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hashlib
import json
import math
import random
import shutil
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from typing import Any

from openpyxl import Workbook

# --------------------------------------------------------------------------- #
# Constants: every literal below is copied from the models, not invented.
# --------------------------------------------------------------------------- #

DEFAULT_SEED = 20261016

# Mirrors apps.instruments.models.AssetType.
ASSET_TYPES = ["equity", "etf", "crypto", "forex", "index", "future"]

# Mirrors apps.ingestion.models.SourceType.
#
# NOTE: the model currently offers only csv/json/api/manual. XLSX is not a
# choice yet. This generator still emits XLSX batches because the ETL is meant
# to support them, and it writes "xlsx" into the synthetic jobs table. The ETL
# milestone needs a small migration adding SourceType.XLSX, or XLSX uploads
# must record an existing choice. Flagged rather than silently assumed.
SOURCE_TYPES = ["csv", "json", "api", "manual"]

# Mirrors apps.ingestion.models.JobStatus.
JOB_STATUSES = ["pending", "processing", "completed", "failed", "completed_with_errors"]

# Mirrors apps.analytics.models.MetricType.
METRIC_TYPES = ["return", "moving_average", "volatility", "drawdown"]

# Venues, with the asset classes that plausibly list on them. The `currency`
# and `asset_types` keys are generator hints; only code/name/timezone map to
# the Exchange model.
EXCHANGES = [
    {"code": "NASDAQ", "name": "Nasdaq Stock Market", "timezone": "America/New_York",
     "currency": "USD", "asset_types": ["equity", "etf", "index"]},
    {"code": "NYSE", "name": "New York Stock Exchange", "timezone": "America/New_York",
     "currency": "USD", "asset_types": ["equity", "etf"]},
    {"code": "LSE", "name": "London Stock Exchange", "timezone": "Europe/London",
     "currency": "GBP", "asset_types": ["equity", "etf"]},
    {"code": "NSE", "name": "National Stock Exchange of India", "timezone": "Asia/Kolkata",
     "currency": "INR", "asset_types": ["equity", "index"]},
    {"code": "CRYPTO", "name": "Synthetic Crypto Network", "timezone": "UTC",
     "currency": "USD", "asset_types": ["crypto"]},
    {"code": "FX", "name": "Synthetic Interbank FX", "timezone": "UTC",
     "currency": "USD", "asset_types": ["forex"]},
]

# Fixed, recognisable instruments so the dirty batch can reference real rows.
# Everything after these is generated. Fields: symbol, name, asset_type,
# exchange_code, currency.
ANCHORS = [
    ("AAPL", "Apple Inc.", "equity", "NASDAQ", "USD"),
    ("MSFT", "Microsoft Corporation", "equity", "NASDAQ", "USD"),
    ("SPY", "SPDR S&P 500 ETF Trust", "etf", "NYSE", "USD"),
    ("GOOG", "Alphabet Inc.", "equity", "NASDAQ", "USD"),
    ("AMZN", "Amazon.com Inc.", "equity", "NASDAQ", "USD"),
    ("TSLA", "Tesla Inc.", "equity", "NASDAQ", "USD"),
    ("NVDA", "NVIDIA Corporation", "equity", "NASDAQ", "USD"),
    {"code": "NASDAQ", "name": "Nasdaq Stock Market", "timezone": "America/New_York",
     "currency": "USD", "asset_types": ["equity", "etf", "index"]},
    {"code": "NYSE", "name": "New York Stock Exchange", "timezone": "America/New_York",
     "currency": "USD", "asset_types": ["equity", "etf"]},
    {"code": "LSE", "name": "London Stock Exchange", "timezone": "Europe/London",
     "currency": "GBP", "asset_types": ["equity", "etf"]},
    {"code": "NSE", "name": "National Stock Exchange of India", "timezone": "Asia/Kolkata",
     "currency": "INR", "asset_types": ["equity", "index"]},
    {"code": "CRYPTO", "name": "Synthetic Crypto Network", "timezone": "UTC",
     "currency": "USD", "asset_types": ["crypto"]},
    {"code": "FX", "name": "Synthetic Interbank FX", "timezone": "UTC",
     "currency": "USD", "asset_types": ["forex"]},
]

# Fixed, recognisable instruments so the dirty batch can reference real rows.
# Everything after these is generated. Fields: symbol, name, asset_type,
# exchange_code, currency.
ANCHORS = [
    ("AAPL", "Apple Inc.", "equity", "NASDAQ", "USD"),
    ("MSFT", "Microsoft Corporation", "equity", "NASDAQ", "USD"),
    ("SPY", "SPDR S&P 500 ETF Trust", "etf", "NYSE", "USD"),
    ("GOOG", "Alphabet Inc.", "equity", "NASDAQ", "USD"),
    ("AMZN", "Amazon.com Inc.", "equity", "NASDAQ", "USD"),
    ("TSLA", "Tesla Inc.", "equity", "NASDAQ", "USD"),
    ("NVDA", "NVIDIA Corporation", "equity", "NASDAQ", "USD"),
]


COMPANY_WORDS = [
    "Apex", "Beacon", "Cobalt", "Delta", "Evergreen", "Frontier", "Granite",
    "Harbor", "Ion", "Juniper", "Keystone", "Lumen", "Meridian", "Northstar",
    "Orion", "Pioneer", "Quantum", "Ridge", "Summit", "Vertex",
]
BUSINESS_WORDS = [
    "Analytics", "Capital", "Dynamics", "Energy", "Financial", "Global",
    "Health", "Industries", "Logistics", "Markets", "Networks", "Robotics",
    "Systems", "Technologies",
    "Analytics", "Capital", "Dynamics", "Energy", "Financial", "Global",
    "Health", "Industries", "Logistics", "Markets", "Networks", "Robotics",
    "Systems", "Technologies",
]

# Month/day pairs treated as market holidays, applied to every venue. Enough to
# make the calendar non-trivial without shipping a real holiday library.
FIXED_HOLIDAYS = {(1, 1), (7, 4), (12, 25)}

CONTENT_TYPES = {
    "csv": "text/csv",
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# The canonical price-batch header. This is what the ETL reads, so it is defined
# once and shared by the clean batches, the dirty batch, and the gap file.
PRICE_COLUMNS = [
    "symbol", "trading_date", "open", "high", "low", "close",
    "adjusted_close", "volume", "source",
]

# Error codes this generator guarantees the dirty batch produces. The ETL's
# validator must reuse these exact strings so the expectations stay checkable.
RULE_CODES = [
    "missing_symbol",
    "missing_required_field",
    "non_positive_price",
    "high_below_low",
    "open_outside_range",
    "close_outside_range",
    "negative_volume",
    "invalid_date",
    "future_date",
    "duplicate_record",
    "unknown_instrument",
]


# --------------------------------------------------------------------------- #
# Scalar helpers
# --------------------------------------------------------------------------- #

def quantize(value: float | str | Decimal, places: int) -> Decimal:
    """Round to `places` decimals using the decimal grid the model stores on.

    Keeping every price on one grid is what lets the 'high >= low' and
    'inside the range' invariants survive rounding: rounding is monotonic, so a
    true ordering cannot become a false one.
    """
    return Decimal(str(value)).quantize(
        Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP
    )
# Month/day pairs treated as market holidays, applied to every venue. Enough to
# make the calendar non-trivial without shipping a real holiday library.
FIXED_HOLIDAYS = {(1, 1), (7, 4), (12, 25)}

CONTENT_TYPES = {
    "csv": "text/csv",
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# The canonical price-batch header. This is what the ETL reads, so it is defined
# once and shared by the clean batches, the dirty batch, and the gap file.
PRICE_COLUMNS = [
    "symbol", "trading_date", "open", "high", "low", "close",
    "adjusted_close", "volume", "source",
]

# Error codes this generator guarantees the dirty batch produces. The ETL's
# validator must reuse these exact strings so the expectations stay checkable.
RULE_CODES = [
    "missing_symbol",
    "missing_required_field",
    "non_positive_price",
    "high_below_low",
    "open_outside_range",
    "close_outside_range",
    "negative_volume",
    "invalid_date",
    "future_date",
    "duplicate_record",
    "unknown_instrument",
]


# --------------------------------------------------------------------------- #
# Scalar helpers
# --------------------------------------------------------------------------- #

def quantize(value: float | str | Decimal, places: int) -> Decimal:
    """Round to `places` decimals using the decimal grid the model stores on.

    Keeping every price on one grid is what lets the 'high >= low' and
    'inside the range' invariants survive rounding: rounding is monotonic, so a
    true ordering cannot become a false one.
    """
    return Decimal(str(value)).quantize(
        Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP
    )


def iso_date(value: date) -> str:
    return value.isoformat()


def iso_timestamp(moment: datetime) -> str:
    """Render a UTC instant as an ISO-8601 string ending in 'Z'."""
    return moment.replace(microsecond=0, tzinfo=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def business_days_ending_on(end: date, count: int) -> list[date]:
    """Return `count` weekdays ending at (and including) `end`, holidays removed.

    Walking backwards from a fixed end date — rather than forwards from a start
    — keeps the sample overlapping the hard-coded dirty rows regardless of how
    many days are requested.
    """
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5 and (cursor.month, cursor.day) not in FIXED_HOLIDAYS:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


# --------------------------------------------------------------------------- #
# Row builders. Each returns dicts whose keys are the documented file columns.
# --------------------------------------------------------------------------- #

def build_exchanges() -> list[dict[str, Any]]:
    """Reference rows for Exchange: code, name, timezone."""
    return [{"code": e["code"], "name": e["name"], "timezone": e["timezone"]} for e in EXCHANGES]


def build_instruments(count: int, rng: random.Random) -> list[dict[str, Any]]:
    """Instrument rows: symbol, name, asset_type, exchange_code, currency, is_active.

    The seven anchors come first so the dirty batch can name them. Remaining
    rows are generated and shuffled so file order is not alphabetical.
    """
    if count < len(ANCHORS):
        raise SystemExit(f"--instruments must be at least {len(ANCHORS)} (the anchor set).")

    instruments: list[dict[str, Any]] = [
        {
            "symbol": symbol,
            "name": name,
            "asset_type": asset_type,
            "exchange_code": exchange_code,
            "currency": currency,
            # Anchors are all live; a later generated row goes inactive so the
            # is_active filter has something to find.
            "is_active": True,
        }
        for symbol, name, asset_type, exchange_code, currency in ANCHORS
    ]

    used_symbols = {row["symbol"] for row in instruments}
    for index in range(count - len(ANCHORS)):
        exchange = EXCHANGES[index % len(EXCHANGES)]
        asset_type = exchange["asset_types"][index % len(exchange["asset_types"])]
def iso_date(value: date) -> str:
    return value.isoformat()


def iso_timestamp(moment: datetime) -> str:
    """Render a UTC instant as an ISO-8601 string ending in 'Z'."""
    return moment.replace(microsecond=0, tzinfo=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def business_days_ending_on(end: date, count: int) -> list[date]:
    """Return `count` weekdays ending at (and including) `end`, holidays removed.

    Walking backwards from a fixed end date — rather than forwards from a start
    — keeps the sample overlapping the hard-coded dirty rows regardless of how
    many days are requested.
    """
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5 and (cursor.month, cursor.day) not in FIXED_HOLIDAYS:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


# --------------------------------------------------------------------------- #
# Row builders. Each returns dicts whose keys are the documented file columns.
# --------------------------------------------------------------------------- #

def build_exchanges() -> list[dict[str, Any]]:
    """Reference rows for Exchange: code, name, timezone."""
    return [{"code": e["code"], "name": e["name"], "timezone": e["timezone"]} for e in EXCHANGES]


def build_instruments(count: int, rng: random.Random) -> list[dict[str, Any]]:
    """Instrument rows: symbol, name, asset_type, exchange_code, currency, is_active.

    The seven anchors come first so the dirty batch can name them. Remaining
    rows are generated and shuffled so file order is not alphabetical.
    """
    if count < len(ANCHORS):
        raise SystemExit(f"--instruments must be at least {len(ANCHORS)} (the anchor set).")

    instruments: list[dict[str, Any]] = [
        {
            "symbol": symbol,
            "name": name,
            "asset_type": asset_type,
            "exchange_code": exchange_code,
            "currency": currency,
            # Anchors are all live; a later generated row goes inactive so the
            # is_active filter has something to find.
            "is_active": True,
        }
        for symbol, name, asset_type, exchange_code, currency in ANCHORS
    ]

    used_symbols = {row["symbol"] for row in instruments}
    for index in range(count - len(ANCHORS)):
        exchange = EXCHANGES[index % len(EXCHANGES)]
        asset_type = exchange["asset_types"][index % len(exchange["asset_types"])]
        prefix = COMPANY_WORDS[index % len(COMPANY_WORDS)][:3].upper()
        symbol = f"{prefix}{index + 1:03d}"
        while symbol in used_symbols:
            symbol = f"{symbol}X"
        used_symbols.add(symbol)
        while symbol in used_symbols:
            symbol = f"{symbol}X"
        used_symbols.add(symbol)
        instruments.append(
            {
                "symbol": symbol,
                "name": f"{COMPANY_WORDS[index % len(COMPANY_WORDS)]} "
                        f"{BUSINESS_WORDS[(index * 3) % len(BUSINESS_WORDS)]} {index + 1}",
                "asset_type": asset_type,
                "exchange_code": exchange["code"],
                "currency": exchange["currency"],
                "is_active": index % 11 != 0,  # roughly one in eleven delisted
                "name": f"{COMPANY_WORDS[index % len(COMPANY_WORDS)]} "
                        f"{BUSINESS_WORDS[(index * 3) % len(BUSINESS_WORDS)]} {index + 1}",
                "asset_type": asset_type,
                "exchange_code": exchange["code"],
                "currency": exchange["currency"],
                "is_active": index % 11 != 0,  # roughly one in eleven delisted
            }
        )


    rng.shuffle(instruments)
    return instruments


def build_calendar(dates: Sequence[date]) -> list[dict[str, Any]]:
    """TradingCalendar rows: date, exchange_code.

    `TradingCalendar.date` is unique, so one row per date is the only shape the
    schema allows — the calendar is global, and `exchange_code` is blank
    (NULL) to say "the market as a whole was open", not a specific venue.
    """
    return [{"date": iso_date(day), "exchange_code": ""} for day in dates]


def iter_instrument_prices(
    instrument: dict[str, Any],
    dates: Sequence[date],
    rng: random.Random,
    places: int,
    closes: dict[str, list[float]],
) -> Iterator[dict[str, Any]]:
    """Yield one clean OHLCV row per trading date for a single instrument.

    Contract
    --------
    in : instrument row, the ordered trading dates, a per-instrument RNG, the
         price grid, and a dict to accumulate closes into.
    out: an iterator of rows keyed by symbol, trading_date, open, high, low,
         close, adjusted_close, volume, source.

    Business purpose: a seeded geometric random walk whose high/low bracket the
    open and close, so the output satisfies the model's CHECK constraints and
    the validator's range rules by construction. Generating per instrument (and
    streaming) keeps peak memory to one instrument's history rather than the
    whole file.
    """
    symbol = instrument["symbol"]
    previous_close = rng.uniform(20.0, 400.0)
    for day in dates:
        daily_return = rng.gauss(0.0002, 0.018)
        opening_gap = rng.gauss(0.0, 0.006)
        open_price = max(0.01, previous_close * (1 + opening_gap))
        close_price = max(0.01, open_price * (1 + daily_return))
        high_price = max(open_price, close_price) * (1 + rng.uniform(0.0005, 0.02))
        low_price = min(open_price, close_price) * (1 - rng.uniform(0.0005, 0.02))
        # ~2% of rows have no adjustment published: a real NULL, not a zero.
        has_adjustment = rng.random() >= 0.02
        volume = max(1, int(rng.lognormvariate(math.log(1_200_000), 0.8)))

        open_q = quantize(open_price, places)
        close_q = quantize(close_price, places)
        yield {
            "symbol": symbol,
            "trading_date": iso_date(day),
            "open": open_q,
            "high": quantize(high_price, places),
            "low": quantize(low_price, places),
            "close": close_q,
            "adjusted_close": quantize(close_price, places) if has_adjustment else None,
            "volume": volume,
            "source": "synthetic_generator",
        }
        closes[symbol].append(float(close_q))
        previous_close = close_price


def build_analytics(
    closes: dict[str, list[float]], last_date: date, lookback: int, places: int
) -> list[dict[str, Any]]:
    """AnalyticSnapshot rows: symbol, as_of_date, metric_type, lookback_days, value, metadata.

    One row per instrument per MetricType, computed from the generated closes,
    so the values are internally consistent with the price files rather than
    arbitrary numbers.
    """
    rows: list[dict[str, Any]] = []
    for symbol, series in sorted(closes.items()):
        if len(series) < 2:
            continue
        window = series[-lookback:]
        returns = [
            (window[i] / window[i - 1]) - 1
            for i in range(1, len(window))
            if window[i - 1]
        ]
        mean_return = sum(returns) / len(returns) if returns else 0.0
        variance = (
            sum((value - mean_return) ** 2 for value in returns) / len(returns)
            if returns
            else 0.0
        )
        volatility = math.sqrt(variance) * math.sqrt(252)
        peak = max(series)
        drawdown = (series[-1] / peak) - 1 if peak else 0.0

        common = {"symbol": symbol, "as_of_date": iso_date(last_date)}
        rows.extend(
            [
                {**common, "metric_type": "return", "lookback_days": 1,
                 "value": quantize(returns[-1] if returns else 0.0, places),
                 "metadata": {"price_field": "close", "observations": 2}},
                {**common, "metric_type": "moving_average", "lookback_days": len(window),
                 "value": quantize(sum(window) / len(window), places),
                 "metadata": {"price_field": "close", "observations": len(window)}},
                {**common, "metric_type": "volatility", "lookback_days": len(window),
                 "value": quantize(volatility, places),
                 "metadata": {"annualization_days": 252, "return_type": "simple"}},
                {**common, "metric_type": "drawdown", "lookback_days": len(series),
                 "value": quantize(drawdown, places),
                 # JSONField cannot hold a Decimal; Django's encoder would
                 # stringify it, so store the exact text ourselves.
                 "metadata": {"peak_close": format(quantize(peak, places), "f")}},
            ]
        )
    return rows


def build_dirty_batch() -> list[tuple[dict[str, Any], str | None]]:
    """Hand-written rows that trip exactly one rule each, with the expected rule.

    Returns `(row, expected_rule)` pairs where `expected_rule` is None for the
    two rows that must be accepted. Hand-written rather than random so the
    expectation table in EXPECTED_RESULTS.md is exact.

    The column order matches the clean batches. A blank string means "absent",
    which is what a real CSV export looks like for a missing value.
    """
    def row(symbol="", trading_date="", open="", high="", low="", close="",
            adjusted_close="", volume="", source="synthetic_dirty"):
        return {
            "symbol": symbol, "trading_date": trading_date, "open": open,
            "high": high, "low": low, "close": close,
            "adjusted_close": adjusted_close, "volume": volume, "source": source,
        }

    return [
        # 1 — valid baseline.
        (row("AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), None),
        # 2 — symbol missing.
        (row("", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), "missing_symbol"),
        # 3 — OHLC field missing (open).
        (row("MSFT", "2024-03-12", "", "102.00", "99.00", "101.00", "101.00", "1000"), "missing_required_field"),
        # 4 — OHLC field missing (low).
        (row("MSFT", "2024-03-13", "100.00", "102.00", "", "101.00", "101.00", "1000"), "missing_required_field"),
        # 5 — zero price is not a price.
        (row("SPY", "2024-03-12", "0.00", "102.00", "99.00", "101.00", "101.00", "1000"), "non_positive_price"),
        # 6 — negative price.
        (row("SPY", "2024-03-13", "100.00", "102.00", "99.00", "-5.00", "101.00", "1000"), "non_positive_price"),
        # 7 — high below low.
        (row("GOOG", "2024-03-12", "100.00", "98.00", "99.00", "101.00", "101.00", "1000"), "high_below_low"),
        # 8 — open above the high.
        (row("AMZN", "2024-03-12", "105.00", "102.00", "99.00", "101.00", "101.00", "1000"), "open_outside_range"),
        # 9 — close above the high.
        (row("AMZN", "2024-03-13", "100.00", "102.00", "99.00", "105.00", "101.00", "1000"), "close_outside_range"),
        # 10 — negative volume.
        (row("TSLA", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", "-100"), "negative_volume"),
        # 11 — impossible calendar date.
        (row("NVDA", "2024-13-45", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), "invalid_date"),
        # 12 — a date after today.
        (row("NVDA", "2099-01-01", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), "future_date"),
        # 13 — byte-identical duplicate of row 1 (same symbol and date).
        (row("AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), "duplicate_record"),
        # 14 — symbol resolves to no instrument.
        (row("ZZZZ", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), "unknown_instrument"),
        # 15 — second valid row, so the batch is partially valid, not all-or-nothing.
        (row("MSFT", "2024-03-14", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), None),
    ]


def build_missing_dates_batch(
    instruments: Sequence[dict[str, Any]], dates: Sequence[date], rng: random.Random,
    places: int, symbol: str, drop: date, closes: dict[str, list[float]],
) -> list[dict[str, Any]]:
    """Clean rows for one instrument with one expected trading day removed.

    Business purpose: exercises the calendar rule, which is an absence rather
    than a bad value and so cannot live in the row-level dirty batch.
    """
    chosen = next((i for i in instruments if i["symbol"] == symbol), None)
    if chosen is None:
        return []
    return [
        record
        for record in iter_instrument_prices(chosen, dates, rng, places, closes)
        if record["trading_date"] != iso_date(drop)
    ]


# --------------------------------------------------------------------------- #
# Writers. One streaming writer per format; none holds a whole table in memory.
# --------------------------------------------------------------------------- #

def json_default(value: Any) -> Any:
    """Fallback encoder for values JSON has no native form for."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def csv_cell(value: Any) -> Any:
    """Render one value for CSV: exact decimal text, ISO dates, JSON for dicts."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=json_default)
    return value


def xlsx_cell(value: Any) -> Any:
    """Render one value for XLSX: numbers stay numeric, dates stay text."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=json_default)
    return value


def json_value(value: Any) -> Any:
    """Render one value for JSON: decimals become numbers, dates become text."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value
def build_calendar(dates: Sequence[date]) -> list[dict[str, Any]]:
    """TradingCalendar rows: date, exchange_code.

    `TradingCalendar.date` is unique, so one row per date is the only shape the
    schema allows — the calendar is global, and `exchange_code` is blank
    (NULL) to say "the market as a whole was open", not a specific venue.
    """
    return [{"date": iso_date(day), "exchange_code": ""} for day in dates]


def iter_instrument_prices(
    instrument: dict[str, Any],
    dates: Sequence[date],
    rng: random.Random,
    places: int,
    closes: dict[str, list[float]],
) -> Iterator[dict[str, Any]]:
    """Yield one clean OHLCV row per trading date for a single instrument.

    Contract
    --------
    in : instrument row, the ordered trading dates, a per-instrument RNG, the
         price grid, and a dict to accumulate closes into.
    out: an iterator of rows keyed by symbol, trading_date, open, high, low,
         close, adjusted_close, volume, source.

    Business purpose: a seeded geometric random walk whose high/low bracket the
    open and close, so the output satisfies the model's CHECK constraints and
    the validator's range rules by construction. Generating per instrument (and
    streaming) keeps peak memory to one instrument's history rather than the
    whole file.
    """
    symbol = instrument["symbol"]
    previous_close = rng.uniform(20.0, 400.0)
    for day in dates:
        daily_return = rng.gauss(0.0002, 0.018)
        opening_gap = rng.gauss(0.0, 0.006)
        open_price = max(0.01, previous_close * (1 + opening_gap))
        close_price = max(0.01, open_price * (1 + daily_return))
        high_price = max(open_price, close_price) * (1 + rng.uniform(0.0005, 0.02))
        low_price = min(open_price, close_price) * (1 - rng.uniform(0.0005, 0.02))
        # ~2% of rows have no adjustment published: a real NULL, not a zero.
        has_adjustment = rng.random() >= 0.02
        volume = max(1, int(rng.lognormvariate(math.log(1_200_000), 0.8)))

        open_q = quantize(open_price, places)
        close_q = quantize(close_price, places)
        yield {
            "symbol": symbol,
            "trading_date": iso_date(day),
            "open": open_q,
            "high": quantize(high_price, places),
            "low": quantize(low_price, places),
            "close": close_q,
            "adjusted_close": quantize(close_price, places) if has_adjustment else None,
            "volume": volume,
            "source": "synthetic_generator",
        }
        closes[symbol].append(float(close_q))
        previous_close = close_price


def build_analytics(
    closes: dict[str, list[float]], last_date: date, lookback: int, places: int
) -> list[dict[str, Any]]:
    """AnalyticSnapshot rows: symbol, as_of_date, metric_type, lookback_days, value, metadata.

    One row per instrument per MetricType, computed from the generated closes,
    so the values are internally consistent with the price files rather than
    arbitrary numbers.
    """
    rows: list[dict[str, Any]] = []
    for symbol, series in sorted(closes.items()):
        if len(series) < 2:
            continue
        window = series[-lookback:]
        returns = [
            (window[i] / window[i - 1]) - 1
            for i in range(1, len(window))
            if window[i - 1]
        ]
        mean_return = sum(returns) / len(returns) if returns else 0.0
        variance = (
            sum((value - mean_return) ** 2 for value in returns) / len(returns)
            if returns
            else 0.0
        )
        volatility = math.sqrt(variance) * math.sqrt(252)
        peak = max(series)
        drawdown = (series[-1] / peak) - 1 if peak else 0.0

        common = {"symbol": symbol, "as_of_date": iso_date(last_date)}
        rows.extend(
            [
                {**common, "metric_type": "return", "lookback_days": 1,
                 "value": quantize(returns[-1] if returns else 0.0, places),
                 "metadata": {"price_field": "close", "observations": 2}},
                {**common, "metric_type": "moving_average", "lookback_days": len(window),
                 "value": quantize(sum(window) / len(window), places),
                 "metadata": {"price_field": "close", "observations": len(window)}},
                {**common, "metric_type": "volatility", "lookback_days": len(window),
                 "value": quantize(volatility, places),
                 "metadata": {"annualization_days": 252, "return_type": "simple"}},
                {**common, "metric_type": "drawdown", "lookback_days": len(series),
                 "value": quantize(drawdown, places),
                 # JSONField cannot hold a Decimal; Django's encoder would
                 # stringify it, so store the exact text ourselves.
                 "metadata": {"peak_close": format(quantize(peak, places), "f")}},
            ]
        )
    return rows


def build_dirty_batch() -> list[tuple[dict[str, Any], str | None]]:
    """Hand-written rows that trip exactly one rule each, with the expected rule.

    Returns `(row, expected_rule)` pairs where `expected_rule` is None for the
    two rows that must be accepted. Hand-written rather than random so the
    expectation table in EXPECTED_RESULTS.md is exact.

    The column order matches the clean batches. A blank string means "absent",
    which is what a real CSV export looks like for a missing value.
    """
    def row(symbol="", trading_date="", open="", high="", low="", close="",
            adjusted_close="", volume="", source="synthetic_dirty"):
        return {
            "symbol": symbol, "trading_date": trading_date, "open": open,
            "high": high, "low": low, "close": close,
            "adjusted_close": adjusted_close, "volume": volume, "source": source,
        }

    return [
        # 1 — valid baseline.
        (row("AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), None),
        # 2 — symbol missing.
        (row("", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), "missing_symbol"),
        # 3 — OHLC field missing (open).
        (row("MSFT", "2024-03-12", "", "102.00", "99.00", "101.00", "101.00", "1000"), "missing_required_field"),
        # 4 — OHLC field missing (low).
        (row("MSFT", "2024-03-13", "100.00", "102.00", "", "101.00", "101.00", "1000"), "missing_required_field"),
        # 5 — zero price is not a price.
        (row("SPY", "2024-03-12", "0.00", "102.00", "99.00", "101.00", "101.00", "1000"), "non_positive_price"),
        # 6 — negative price.
        (row("SPY", "2024-03-13", "100.00", "102.00", "99.00", "-5.00", "101.00", "1000"), "non_positive_price"),
        # 7 — high below low.
        (row("GOOG", "2024-03-12", "100.00", "98.00", "99.00", "101.00", "101.00", "1000"), "high_below_low"),
        # 8 — open above the high.
        (row("AMZN", "2024-03-12", "105.00", "102.00", "99.00", "101.00", "101.00", "1000"), "open_outside_range"),
        # 9 — close above the high.
        (row("AMZN", "2024-03-13", "100.00", "102.00", "99.00", "105.00", "101.00", "1000"), "close_outside_range"),
        # 10 — negative volume.
        (row("TSLA", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", "-100"), "negative_volume"),
        # 11 — impossible calendar date.
        (row("NVDA", "2024-13-45", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), "invalid_date"),
        # 12 — a date after today.
        (row("NVDA", "2099-01-01", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), "future_date"),
        # 13 — byte-identical duplicate of row 1 (same symbol and date).
        (row("AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), "duplicate_record"),
        # 14 — symbol resolves to no instrument.
        (row("ZZZZ", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), "unknown_instrument"),
        # 15 — second valid row, so the batch is partially valid, not all-or-nothing.
        (row("MSFT", "2024-03-14", "100.00", "102.00", "99.00", "101.00", "101.00", "1000"), None),
    ]


def build_missing_dates_batch(
    instruments: Sequence[dict[str, Any]], dates: Sequence[date], rng: random.Random,
    places: int, symbol: str, drop: date, closes: dict[str, list[float]],
) -> list[dict[str, Any]]:
    """Clean rows for one instrument with one expected trading day removed.

    Business purpose: exercises the calendar rule, which is an absence rather
    than a bad value and so cannot live in the row-level dirty batch.
    """
    chosen = next((i for i in instruments if i["symbol"] == symbol), None)
    if chosen is None:
        return []
    return [
        record
        for record in iter_instrument_prices(chosen, dates, rng, places, closes)
        if record["trading_date"] != iso_date(drop)
    ]


# --------------------------------------------------------------------------- #
# Writers. One streaming writer per format; none holds a whole table in memory.
# --------------------------------------------------------------------------- #

def json_default(value: Any) -> Any:
    """Fallback encoder for values JSON has no native form for."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def csv_cell(value: Any) -> Any:
    """Render one value for CSV: exact decimal text, ISO dates, JSON for dicts."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=json_default)
    return value


def xlsx_cell(value: Any) -> Any:
    """Render one value for XLSX: numbers stay numeric, dates stay text."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=json_default)
    return value


def json_value(value: Any) -> Any:
    """Render one value for JSON: decimals become numbers, dates become text."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    """Stream `rows` to a CSV file with `columns` as the header."""
def write_csv(path: Path, columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    """Stream `rows` to a CSV file with `columns` as the header."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow({name: csv_cell(row.get(name)) for name in columns})
        for row in rows:
            writer.writerow({name: csv_cell(row.get(name)) for name in columns})


def write_json(path: Path, columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    """Stream `rows` to a JSON array of objects without buffering the array."""
def write_json(path: Path, columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    """Stream `rows` to a JSON array of objects without buffering the array."""
    with path.open("w", encoding="utf-8") as handle:
        handle.write("[\n")
        first = True
        for row in rows:
            record = {name: json_value(row.get(name)) for name in columns}
            handle.write("" if first else ",\n")
            handle.write("  " + json.dumps(record, ensure_ascii=False, default=json_default))
            first = False
        handle.write("\n]\n")

def write_xlsx(
    path: Path, columns: Sequence[str], rows: Iterable[dict[str, Any]], sheet_name: str
) -> None:
    """Stream `rows` to a single-sheet XLSX workbook in write-only mode."""
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title=sheet_name[:31])
    sheet.append([str(name) for name in columns])
    for row in rows:
        sheet.append([xlsx_cell(row.get(name)) for name in columns])
    workbook.save(path)


def write_table(
    directory: Path, name: str, columns: Sequence[str],
    factory: Callable[[], Iterable[dict[str, Any]]], formats: Sequence[str],
) -> dict[str, int]:
    """Write one table in every requested format and return {format: byte size}.

    `factory` is called once per format rather than `rows` being reused, so a
    non-rewindable generator can feed all three writers with no buffering.
    """
    sizes: dict[str, int] = {}
    for fmt in formats:
        path = directory / f"{name}.{fmt}"
        rows = factory()
        if fmt == "csv":
            write_csv(path, columns, rows)
        elif fmt == "json":
            write_json(path, columns, rows)
        elif fmt == "xlsx":
            write_xlsx(path, columns, rows, name)
        else:
            raise ValueError(f"Unsupported format: {fmt}")
        sizes[fmt] = path.stat().st_size
    return sizes


# --------------------------------------------------------------------------- #
# CLI and orchestration
# --------------------------------------------------------------------------- #

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse and validate the command line. See the module docstring for output."""
    parser = argparse.ArgumentParser(
        description="Generate synthetic market-data fixtures matching the Django models.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("sample_data/synthetic"))
    parser.add_argument("--instruments", type=int, default=12,
                        help=f"Total instruments, including {len(ANCHORS)} anchors (default: 12).")
    parser.add_argument("--trading-days", type=int, default=30,
                        help="Weekday history depth per instrument (default: 30).")
    parser.add_argument("--price-files", type=int, default=6,
                        help="Clean price batches to split instruments across (default: 6).")
    parser.add_argument("--end-date", type=date.fromisoformat, default=date(2024, 3, 14),
                        help="Last trading day in the sample, ISO (default: 2024-03-14).")
    parser.add_argument("--price-decimals", type=int, default=4,
                        help="Decimal places for prices; the model stores 8 (default: 4).")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help="Random seed; the same seed reproduces every value.")
    parser.add_argument("--keep-existing", action="store_true",
                        help="Do not delete an existing output directory first.")
    args = parser.parse_args(argv)

    if args.instruments < len(ANCHORS):
        parser.error(f"--instruments must be at least {len(ANCHORS)}.")
    if args.trading_days < 2:
        parser.error("--trading-days must be at least 2.")
    if args.price_files < 1:
        parser.error("--price-files must be at least 1.")
    if not 0 <= args.price_decimals <= 8:
        parser.error("--price-decimals must be between 0 and 8 (the model stores 8).")
    return args


def split_across(instruments: Sequence[dict[str, Any]], parts: int) -> list[list[dict[str, Any]]]:
    """Deal instruments round-robin into `parts` non-empty batches."""
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(parts)]
    for index, instrument in enumerate(instruments):
        buckets[index % parts].append(instrument)
    return [bucket for bucket in buckets if bucket]


def render_expected_results(
    args: argparse.Namespace,
    dates: Sequence[date],
    batches: Sequence[Sequence[dict[str, Any]]],
    dirty_pairs: Sequence[tuple[dict[str, Any], str | None]],
    missing_symbol: str,
    missing_date: date,
) -> str:
    """Build the EXPECTED_RESULTS.md body: the ORM rows each file implies."""
    total_prices = sum(len(batch) for batch in batches) * len(dates)
    accepted = [row for row, rule in dirty_pairs if rule is None]
    rejected = [row for row, rule in dirty_pairs if rule is not None]
    per_rule: dict[str, int] = defaultdict(int)
    for _, rule in dirty_pairs:
        if rule:
            per_rule[rule] += 1

    lines: list[str] = []
    lines.append("# Expected database results\n")
    lines.append("Generated by `scripts/generate_dummy_market_data.py`. "
                 f"Seed `{args.seed}`, {len(dates)} trading days ending "
                 f"{args.end_date.isoformat()}.\n")
    lines.append("## Clean dataset\n")
    lines.append("| Model | Source file(s) | Expected rows |")
    lines.append("|---|---|---|")
    lines.append(f"| `Exchange` | `reference/exchanges.*` | {len(EXCHANGES)} |")
    lines.append(f"| `Instrument` | `reference/instruments.*` | {args.instruments} |")
    lines.append(f"| `TradingCalendar` | `reference/trading_calendar.*` | {len(dates)} |")
    lines.append(f"| `PriceRecord` | `batches/prices_batch_*.{{csv,json,xlsx}}` | {total_prices} |")
    lines.append(f"| `IngestionJob` | `reference/ingestion_jobs.*` | {len(batches)} |")
    lines.append(f"| `IngestionFile` | `reference/ingestion_files.*` | {len(batches)} |")
    lines.append("| `IngestionValidationError` | clean batches | 0 |")
    lines.append(f"| `AnalyticSnapshot` | `reference/analytic_snapshots.*` | {args.instruments * 4} |")
    lines.append(f"| `AuditEvent` | `reference/audit_events.*` | {len(batches)} |")
    lines.append("")
    lines.append("Each clean `IngestionJob` should finish `completed` with "
                 "`rows_received == rows_accepted` and `rows_rejected == 0`, "
                 "and each `IngestionFile` checksum must match the bytes on disk.\n")

    lines.append("## Dirty batch (`dirty/prices_dirty.*`)\n")
    lines.append("| Row | Rule exercised | Expected result |")
    lines.append("|---|---|---|")
    for index, (_, rule) in enumerate(dirty_pairs, start=1):
        outcome = "accepted" if rule is None else "rejected"
        lines.append(f"| {index} | `{rule or 'valid row'}` | {outcome} |")
    lines.append("")
    lines.append(f"The resulting job should be **`completed_with_errors`**: "
                 f"`rows_received = {len(dirty_pairs)}`, "
                 f"`rows_accepted = {len(accepted)}`, "
                 f"`rows_rejected = {len(rejected)}`, with one "
                 f"`IngestionValidationError` per rejected row and `error_code` "
                 f"matching the rule column above.\n")
    lines.append("| `error_code` | Expected count |")
    lines.append("|---|---|")
    for code in RULE_CODES:
        lines.append(f"| `{code}` | {per_rule.get(code, 0)} |")
    lines.append("")

    lines.append("## Missing trading day (`dirty/prices_missing_dates.csv`)\n")
    lines.append(f"`{missing_symbol}` is present in `TradingCalendar` for "
                 f"{missing_date.isoformat()} but has no price row for it. The "
                 "validator's calendar rule should report exactly one "
                 "`missing_trading_date` finding for "
                 f"`{missing_symbol}` on {missing_date.isoformat()}.\n")

    lines.append("## Cross-file duplicate\n")
    lines.append("Row 1 of the dirty batch reuses `AAPL @ 2024-03-12`, a date "
                 "that also exists in the clean batches. Re-ingesting it must "
                 "not create a second `PriceRecord`: the "
                 "`uniq_price_instrument_trading_date` constraint should turn it "
                 "into an update or a `duplicate_record` finding, never a new "
                 "row.\n")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    """Generate every file described in the module docstring."""
    args = parse_args(argv)
    if args.output_dir.exists() and not args.keep_existing:
        shutil.rmtree(args.output_dir)
    reference_dir = args.output_dir / "reference"
    batches_dir = args.output_dir / "batches"
    dirty_dir = args.output_dir / "dirty"
    for directory in (reference_dir, batches_dir, dirty_dir):
        directory.mkdir(parents=True, exist_ok=True)
    reference_dir = args.output_dir / "reference"
    batches_dir = args.output_dir / "batches"
    dirty_dir = args.output_dir / "dirty"
    for directory in (reference_dir, batches_dir, dirty_dir):
        directory.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    dates = business_days_ending_on(args.end_date, args.trading_days)
    instruments = build_instruments(args.instruments, rng)
    batches = split_across(instruments, args.price_files)

    # Closes are accumulated while batches are written, then reused for analytics.
    closes: dict[str, list[float]] = defaultdict(list)
    files: list[dict[str, Any]] = []
    dates = business_days_ending_on(args.end_date, args.trading_days)
    instruments = build_instruments(args.instruments, rng)
    batches = split_across(instruments, args.price_files)

    # Closes are accumulated while batches are written, then reused for analytics.
    closes: dict[str, list[float]] = defaultdict(list)
    files: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []

    for index, batch in enumerate(batches, start=1):
        extension = ("csv", "json", "xlsx")[(index - 1) % 3]
        filename = f"prices_batch_{index:03d}.{extension}"
        path = batches_dir / filename
        columns = PRICE_COLUMNS

        # Each batch is written exactly once, in its own format, so the closes
        # accumulator sees every instrument exactly once. The per-instrument RNG
        # is seeded by symbol, so a rerun reproduces identical prices without
        # carrying state between batches.
        def rows(batch=batch):
            for instrument in batch:
                per_instrument = random.Random(f"{args.seed}:{instrument['symbol']}")
                yield from iter_instrument_prices(
                    instrument, dates, per_instrument, args.price_decimals, closes
                )

        if extension == "csv":
            write_csv(path, columns, rows())
        elif extension == "json":
            write_json(path, columns, rows())
        else:
            write_xlsx(path, columns, rows(), "PriceRecords")

        checksum = hashlib.sha256(path.read_bytes()).hexdigest()

    for index, batch in enumerate(batches, start=1):
        extension = ("csv", "json", "xlsx")[(index - 1) % 3]
        filename = f"prices_batch_{index:03d}.{extension}"
        path = batches_dir / filename
        columns = PRICE_COLUMNS

        # Each batch is written exactly once, in its own format, so the closes
        # accumulator sees every instrument exactly once. The per-instrument RNG
        # is seeded by symbol, so a rerun reproduces identical prices without
        # carrying state between batches.
        def rows(batch=batch):
            for instrument in batch:
                per_instrument = random.Random(f"{args.seed}:{instrument['symbol']}")
                yield from iter_instrument_prices(
                    instrument, dates, per_instrument, args.price_decimals, closes
                )

        if extension == "csv":
            write_csv(path, columns, rows())
        elif extension == "json":
            write_json(path, columns, rows())
        else:
            write_xlsx(path, columns, rows(), "PriceRecords")

        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"market-data-job:{args.seed}:{index}"))
        rows_in_batch = len(batch) * len(dates)
        rows_in_batch = len(batch) * len(dates)
        jobs.append(
            {
                "job_id": job_id,
                "created_by_email": "analyst@example.test",
                "source_type": extension,
                "status": "completed",
                "requested_at": iso_timestamp(datetime.combine(dates[0], time(9, 30)) + timedelta(seconds=index)),
                "started_at": iso_timestamp(datetime.combine(dates[0], time(9, 30)) + timedelta(seconds=index + 1)),
                "completed_at": iso_timestamp(datetime.combine(dates[-1], time(16, 0)) + timedelta(seconds=index)),
                "rows_received": rows_in_batch,
                "rows_accepted": rows_in_batch,
                "rows_rejected": 0,
                "error_summary": "",
            }
        )
        files.append(
            {
                "file_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"market-data-file:{args.seed}:{index}")),
                "job_id": job_id,
                "original_filename": filename,
                "storage_path": f"media/ingestion/{filename}",
                "checksum": checksum,
                "content_type": CONTENT_TYPES[extension],
            }
        )

    def job_source_types() -> Iterator[dict[str, Any]]:
        yield from jobs

    # Anchor symbols cannot be "unknown"; that role falls to ZZZZ in the batch.
    dirty_pairs = build_dirty_batch()
    dirty_columns = PRICE_COLUMNS
    missing_symbol = "MSFT"
    missing_date = dates[-2] if len(dates) >= 2 else dates[-1]
    missing_rows = build_missing_dates_batch(
        instruments, dates, random.Random(f"{args.seed}:missing"), args.price_decimals,
        missing_symbol, missing_date, defaultdict(list),
    )

    analytics = build_analytics(closes, dates[-1], min(20, len(dates)), args.price_decimals)
    files.append(
            {
                "file_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"market-data-file:{args.seed}:{index}")),
                "job_id": job_id,
                "original_filename": filename,
                "storage_path": f"media/ingestion/{filename}",
                "checksum": checksum,
                "content_type": CONTENT_TYPES[extension],
            }
        )

    def job_source_types() -> Iterator[dict[str, Any]]:
        yield from jobs

    # Anchor symbols cannot be "unknown"; that role falls to ZZZZ in the batch.
    dirty_pairs = build_dirty_batch()
    dirty_columns = PRICE_COLUMNS
    missing_symbol = "MSFT"
    missing_date = dates[-2] if len(dates) >= 2 else dates[-1]
    missing_rows = build_missing_dates_batch(
        instruments, dates, random.Random(f"{args.seed}:missing"), args.price_decimals,
        missing_symbol, missing_date, defaultdict(list),
    )

    analytics = build_analytics(closes, dates[-1], min(20, len(dates)), args.price_decimals)
    audit_events = [
        {
            "user_email": job["created_by_email"],
            "action": "ingestion_job.succeeded",
            "object_type": "IngestionJob",
            "object_id": job["job_id"],
            "request_id": f"req-{index:05d}",
            "ip_address": "127.0.0.1",
            "metadata": {"rows_accepted": job["rows_accepted"], "source_type": job["source_type"]},
            "metadata": {"rows_accepted": job["rows_accepted"], "source_type": job["source_type"]},
            "created_at": job["completed_at"],
        }
        for index, job in enumerate(jobs, start=1)
    ]

    table_sizes: dict[str, dict[str, int]] = {}
    reference_tables = [
        ("exchanges", ["code", "name", "timezone"], build_exchanges, "Exchanges"),
        ("instruments",
         ["symbol", "name", "asset_type", "exchange_code", "currency", "is_active"],
         lambda: instruments, "Instruments"),
        ("trading_calendar", ["date", "exchange_code"], lambda: build_calendar(dates), "TradingCalendar"),
        ("ingestion_jobs",
         ["job_id", "created_by_email", "source_type", "status", "requested_at",
          "started_at", "completed_at", "rows_received", "rows_accepted",
          "rows_rejected", "error_summary"],
         lambda: list(job_source_types()), "IngestionJobs"),
        ("ingestion_files",
         ["file_id", "job_id", "original_filename", "storage_path", "checksum", "content_type"],
         lambda: files, "IngestionFiles"),
        ("analytic_snapshots",
         ["symbol", "as_of_date", "metric_type", "lookback_days", "value", "metadata"],
         lambda: analytics, "AnalyticSnapshots"),
        ("audit_events",
         ["user_email", "action", "object_type", "object_id", "request_id",
          "ip_address", "metadata", "created_at"],
         lambda: audit_events, "AuditEvents"),
    ]
    for name, columns, factory, sheet in reference_tables:
        sizes = write_table(reference_dir, name, columns, factory, ("csv", "json", "xlsx"))
        table_sizes[name] = sizes

    write_table(dirty_dir, "prices_dirty", dirty_columns, lambda: (row for row, _ in dirty_pairs),
                ("csv", "json", "xlsx"))
    write_csv(dirty_dir / "prices_missing_dates.csv", dirty_columns, missing_rows)

    manifest = {
        "generator": "scripts/generate_dummy_market_data.py",
        "seed": args.seed,
        "end_date": args.end_date.isoformat(),
        "trading_days": len(dates),
        "instruments": len(instruments),
        "price_batches": len(batches),
        "price_records": sum(len(batch) for batch in batches) * len(dates),
        "price_decimals": args.price_decimals,
        "formats": ["csv", "json", "xlsx"],
        "tables": table_sizes,
        "rule_codes": RULE_CODES,
        "note": "All values are synthetic and unsuitable for any real decision.",
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (args.output_dir / "EXPECTED_RESULTS.md").write_text(
        render_expected_results(args, dates, batches, dirty_pairs, missing_symbol, missing_date),
        encoding="utf-8",
    )

    print(f"Instruments : {len(instruments)}")
    print(f"Trading days: {len(dates)}  ({dates[0]:%Y-%m-%d} to {dates[-1]:%Y-%m-%d})")
    print(f"Price rows  : {manifest['price_records']:,} across {len(batches)} batches")
    print(f"Written to  : {args.output_dir}")


if __name__ == "__main__":
    main()
