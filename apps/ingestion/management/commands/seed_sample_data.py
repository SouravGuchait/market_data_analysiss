"""``manage.py seed_sample_data`` — load the sample dataset's reference rows.

The committed sample price batches reference instruments by symbol, so the
dimension tables and the trading calendar must exist before ``ingest_prices`` can
resolve a row. This command loads exchanges, instruments, and the calendar from
``sample_data/synthetic/reference/`` (the files
``scripts/generate_dummy_market_data.py`` writes).

It is **idempotent**: a code, a symbol, or a date that already exists is updated
in place, so it can be re-run after a database reset or alongside reference data
that is already there, without creating duplicates.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.instruments.models import Exchange, Instrument
from apps.marketdata.models import TradingCalendar

DEFAULT_REFERENCE_DIR = Path("sample_data") / "synthetic" / "reference"


def _read_rows(path: Path) -> list[dict[str, str]]:
    """Read a reference CSV into a list of column-name keyed dicts."""
    if not path.is_file():
        raise CommandError(f"missing reference file: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


class Command(BaseCommand):
    help = (
        "Seed exchanges, instruments, and the trading calendar from the committed "
        "sample dataset (idempotent)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reference-dir",
            default=str(DEFAULT_REFERENCE_DIR),
            help="Directory holding exchanges.csv, instruments.csv and trading_calendar.csv.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        directory = Path(options["reference_dir"])

        exchanges = self._seed_exchanges(directory / "exchanges.csv")
        instruments = self._seed_instruments(directory / "instruments.csv", exchanges)
        calendar = self._seed_calendar(directory / "trading_calendar.csv")

        self.stdout.write(
            self.style.SUCCESS(
                f"seeded {len(exchanges)} exchanges, {instruments} instruments, "
                f"{calendar} trading-calendar days"
            )
        )

    def _seed_exchanges(self, path: Path) -> dict[str, Exchange]:
        by_code: dict[str, Exchange] = {}
        for row in _read_rows(path):
            exchange, _ = Exchange.objects.update_or_create(
                code=row["code"],
                defaults={"name": row["name"], "timezone": row["timezone"]},
            )
            by_code[exchange.code] = exchange
        return by_code

    def _seed_instruments(self, path: Path, exchanges: dict[str, Exchange]) -> int:
        count = 0
        for row in _read_rows(path):
            exchange = exchanges.get(row["exchange_code"])
            if exchange is None:
                raise CommandError(
                    f"instrument {row['symbol']} references unknown exchange "
                    f"{row['exchange_code']!r}"
                )
            Instrument.objects.update_or_create(
                symbol=row["symbol"],
                defaults={
                    "name": row["name"],
                    "asset_type": row["asset_type"],
                    "exchange": exchange,
                    "currency": row["currency"],
                    "is_active": row["is_active"].strip().lower() == "true",
                },
            )
            count += 1
        return count

    def _seed_calendar(self, path: Path) -> int:
        count = 0
        for row in _read_rows(path):
            TradingCalendar.objects.get_or_create(date=date.fromisoformat(row["date"]))
            count += 1
        return count
