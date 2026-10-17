"""Shared builders for the ingestion ETL tests.

The tests need three kinds of fixture: a source file in each supported format, a
handful of reference rows (a user, instruments, a calendar), and ready-made
normalised rows for exercising a rule in isolation. Keeping them here means a
test reads as its intent rather than as setup.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from openpyxl import Workbook

from apps.ingestion.etl.schemas import NormalizedRow
from apps.instruments.models import AssetType, Exchange, Instrument
from apps.marketdata.models import TradingCalendar

# Repository root, three levels up from this file (apps/ingestion/tests/helpers.py).
REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_DIR = REPO_ROOT / "sample_data" / "synthetic"

PRICE_HEADER = [
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


def make_user(email: str = "analyst@example.test"):
    """A job owner. ``IngestionJob.created_by`` is non-nullable, so every job needs one."""
    return get_user_model().objects.create_user(email=email, password="test-pass-123")


def price_row(
    symbol="AAPL",
    trading_date="2024-03-12",
    open_="100.00",
    high="102.00",
    low="99.00",
    close="101.00",
    adjusted_close="101.00",
    volume=1000,
    source="test",
):
    """One row in canonical order, ready to write to any format."""
    return [symbol, trading_date, open_, high, low, close, adjusted_close, volume, source]


def write_csv(path, rows, header=PRICE_HEADER):
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def write_json(path, records):
    path = Path(path)
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def write_xlsx(path, rows, header=PRICE_HEADER, sheet_title="prices"):
    path = Path(path)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path


def make_exchange(code="NASDAQ", name="Nasdaq", timezone="America/New_York"):
    return Exchange.objects.create(code=code, name=name, timezone=timezone)


def make_instrument(symbol, exchange=None, asset_type=AssetType.EQUITY):
    return Instrument.objects.create(
        symbol=symbol,
        name=f"{symbol} Inc.",
        asset_type=asset_type,
        exchange=exchange or make_exchange(code=f"EX{symbol}"),
        currency="USD",
    )


def add_calendar(*dates):
    return TradingCalendar.objects.bulk_create([TradingCalendar(date=day) for day in dates])


def make_row(**overrides) -> NormalizedRow:
    """A clean normalised row that a test can spoil one field of.

    A clean row is the right default: a rule test should fail only because of the
    one field it changed, not because of unrelated noise in the fixture.
    """
    values = {
        "row_number": 1,
        "symbol": "AAPL",
        "trading_date": date(2024, 3, 12),
        "open": Decimal("100.00"),
        "high": Decimal("102.00"),
        "low": Decimal("99.00"),
        "close": Decimal("101.00"),
        "adjusted_close": Decimal("101.00"),
        "volume": 1000,
        "source": "test",
        "raw": {},
        "issues": [],
    }
    values.update(overrides)
    return NormalizedRow(**values)


def read_sample_csv(path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def seed_sample_dataset():
    """Create the exchanges, instruments, and calendar the sample dataset expects.

    The price batches reference instruments by symbol, so an end-to-end run of the
    committed sample needs those instruments to exist first. Reading the
    generator's own reference files keeps the two in step.
    """
    reference = SAMPLE_DIR / "reference"

    exchanges: dict[str, Exchange] = {}
    for row in read_sample_csv(reference / "exchanges.csv"):
        exchanges[row["code"]] = Exchange.objects.create(
            code=row["code"], name=row["name"], timezone=row["timezone"]
        )

    for row in read_sample_csv(reference / "instruments.csv"):
        Instrument.objects.create(
            symbol=row["symbol"],
            name=row["name"],
            asset_type=row["asset_type"],
            exchange=exchanges[row["exchange_code"]],
            currency=row["currency"],
            is_active=row["is_active"].strip().lower() == "true",
        )

    TradingCalendar.objects.bulk_create(
        [
            TradingCalendar(date=date.fromisoformat(row["date"]))
            for row in read_sample_csv(reference / "trading_calendar.csv")
        ]
    )
    return exchanges
