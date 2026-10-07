"""Generate deterministic market-data fixtures for local ingestion development.

The generator produces separate CSV, JSON, and XLSX price batches plus supporting
files for the core database entities. Price batches use the canonical ingestion
schema: symbol, trading_date, open, high, low, close, adjusted_close, volume,
source, and ingestion_job_id. The application should resolve symbol to Instrument.

Examples:
    python scripts/generate_dummy_market_data.py
    python scripts/generate_dummy_market_data.py --instruments 100 --trading-days 2520
    python scripts/generate_dummy_market_data.py --output-dir sample_data/generated --seed 7
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import shutil
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Sequence

from openpyxl import Workbook


CANONICAL_PRICE_COLUMNS = [
    "symbol",
    "trading_date",
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
    "source",
    "ingestion_job_id",
]

EXCHANGES = [
    ("NASDAQ", "USD"),
    ("NYSE", "USD"),
    ("LSE", "GBP"),
    ("NSE", "INR"),
]
COMPANY_WORDS = [
    "Apex", "Beacon", "Cobalt", "Delta", "Evergreen", "Frontier", "Granite",
    "Harbor", "Ion", "Juniper", "Keystone", "Lumen", "Meridian", "Northstar",
    "Orion", "Pioneer", "Quantum", "Ridge", "Summit", "Vertex",
]
BUSINESS_WORDS = [
    "Analytics", "Capital", "Dynamics", "Energy", "Financial", "Global", "Health",
    "Industries", "Logistics", "Markets", "Networks", "Robotics", "Systems", "Tech",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate synthetic market-data ingestion fixtures.")
    parser.add_argument("--output-dir", type=Path, default=Path("sample_data/generated"))
    parser.add_argument("--instruments", type=int, default=40, help="Number of instruments (default: 40).")
    parser.add_argument("--trading-days", type=int, default=1_260, help="Business days per instrument (default: 1260).")
    parser.add_argument("--price-files", type=int, default=12, help="Number of split price batches (default: 12).")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2021, 1, 4))
    parser.add_argument("--seed", type=int, default=20261007, help="Random seed for reproducible output.")
    parser.add_argument("--include-invalid-sample", action="store_true", help="Add one intentionally invalid CSV batch for validation tests.")
    parser.add_argument("--keep-existing", action="store_true", help="Do not delete an existing output directory.")
    return parser.parse_args()


def money(value: float) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def iso_timestamp(day: date, offset_seconds: int = 0) -> str:
    return datetime.combine(day, time(9, 30), tzinfo=timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z") if offset_seconds == 0 else (
        datetime.combine(day, time(9, 30), tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def business_dates(start: date, count: int) -> list[date]:
    dates: list[date] = []
    current = start
    while len(dates) < count:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def make_instruments(count: int, rng: random.Random) -> list[dict[str, Any]]:
    instruments: list[dict[str, Any]] = []
    for index in range(count):
        exchange, currency = EXCHANGES[index % len(EXCHANGES)]
        prefix = COMPANY_WORDS[index % len(COMPANY_WORDS)][:3].upper()
        symbol = f"{prefix}{index + 1:03d}"
        instruments.append(
            {
                "instrument_id": index + 1,
                "symbol": symbol,
                "name": f"{COMPANY_WORDS[index % len(COMPANY_WORDS)]} {BUSINESS_WORDS[(index * 3) % len(BUSINESS_WORDS)]} {index + 1}",
                "asset_class": "EQUITY" if index % 5 else "ETF",
                "exchange_code": exchange,
                "currency": currency,
                "is_active": True,
                "created_at": iso_timestamp(date(2021, 1, 1), index),
            }
        )
    rng.shuffle(instruments)
    return instruments


def make_price_records(
    instruments: Sequence[dict[str, Any]], dates: Sequence[date], rng: random.Random
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for instrument in instruments:
        previous_close = rng.uniform(25.0, 450.0)
        for trading_day in dates:
            daily_return = rng.gauss(0.00025, 0.018)
            opening_gap = rng.gauss(0.0, 0.006)
            open_price = max(0.01, previous_close * (1 + opening_gap))
            close_price = max(0.01, open_price * (1 + daily_return))
            high_price = max(open_price, close_price) * (1 + rng.uniform(0.0005, 0.018))
            low_price = min(open_price, close_price) * (1 - rng.uniform(0.0005, 0.018))
            adjusted_close = close_price * (1 - (0.0001 if rng.random() < 0.015 else 0.0))
            volume = max(1_000, int(rng.lognormvariate(math.log(1_500_000), 0.8)))
            records.append(
                {
                    "symbol": instrument["symbol"],
                    "trading_date": trading_day.isoformat(),
                    "open": money(open_price),
                    "high": money(high_price),
                    "low": money(low_price),
                    "close": money(close_price),
                    "adjusted_close": money(adjusted_close),
                    "volume": volume,
                    "source": "synthetic_generator",
                    "ingestion_job_id": "",  # Populated when records are assigned to a batch.
                }
            )
            previous_close = close_price
    records.sort(key=lambda row: (row["symbol"], row["trading_date"]))
    return records


def split_records(records: Sequence[dict[str, Any]], parts: int) -> list[list[dict[str, Any]]]:
    batches = [[] for _ in range(parts)]
    for index, record in enumerate(records):
        batches[index % parts].append(record)
    return batches


def write_csv(path: Path, rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=2)


def write_xlsx(path: Path, rows: Sequence[dict[str, Any]], columns: Sequence[str], sheet_name: str) -> None:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(sheet_name)
    sheet.append(list(columns))
    for row in rows:
        sheet.append([row.get(column) for column in columns])
    workbook.save(path)


def make_analytics(records: Sequence[dict[str, Any]], instruments: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_symbol[record["symbol"]].append(record)
    analytics: list[dict[str, Any]] = []
    instrument_ids = {item["symbol"]: item["instrument_id"] for item in instruments}
    for symbol, rows in by_symbol.items():
        closes = [float(row["adjusted_close"]) for row in rows]
        if len(closes) < 21:
            continue
        returns = [(closes[index] / closes[index - 1]) - 1 for index in range(1, len(closes))]
        lookback = min(20, len(closes))
        recent_returns = returns[-lookback:]
        mean_return = sum(recent_returns) / len(recent_returns)
        volatility = (sum((value - mean_return) ** 2 for value in recent_returns) / len(recent_returns)) ** 0.5 * math.sqrt(252)
        rolling_high = max(closes)
        analytics.extend(
            [
                {
                    "instrument_id": instrument_ids[symbol],
                    "symbol": symbol,
                    "as_of_date": rows[-1]["trading_date"],
                    "metric_type": "SMA",
                    "lookback_days": lookback,
                    "value": money(sum(closes[-lookback:]) / lookback),
                    "metadata": json.dumps({"price_field": "adjusted_close", "observation_count": lookback}),
                    "calculated_at": iso_timestamp(date.fromisoformat(rows[-1]["trading_date"])),
                },
                {
                    "instrument_id": instrument_ids[symbol],
                    "symbol": symbol,
                    "as_of_date": rows[-1]["trading_date"],
                    "metric_type": "ANNUALIZED_VOLATILITY",
                    "lookback_days": lookback,
                    "value": money(volatility),
                    "metadata": json.dumps({"annualization_days": 252, "return_type": "simple"}),
                    "calculated_at": iso_timestamp(date.fromisoformat(rows[-1]["trading_date"])),
                },
                {
                    "instrument_id": instrument_ids[symbol],
                    "symbol": symbol,
                    "as_of_date": rows[-1]["trading_date"],
                    "metric_type": "DRAWDOWN",
                    "lookback_days": len(closes),
                    "value": money((closes[-1] / rolling_high) - 1),
                    "metadata": json.dumps({"peak_adjusted_close": money(rolling_high)}),
                    "calculated_at": iso_timestamp(date.fromisoformat(rows[-1]["trading_date"])),
                },
            ]
        )
    return analytics


def generate_invalid_sample(path: Path) -> None:
    rows = [
        {"symbol": "", "trading_date": "2025-13-40", "open": "10", "high": "9", "low": "11", "close": "-2", "volume": "-50"},
        {"symbol": "APX001", "trading_date": "2025-01-02", "open": "100", "high": "101", "low": "99", "close": "100", "volume": "1000"},
    ]
    write_csv(path, rows, list(rows[0].keys()))


def main() -> None:
    args = parse_args()
    if args.instruments < 1 or args.trading_days < 2 or args.price_files < 1:
        raise SystemExit("--instruments and --price-files must be positive; --trading-days must be at least 2.")
    if args.output_dir.exists() and not args.keep_existing:
        shutil.rmtree(args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    instruments = make_instruments(args.instruments, rng)
    dates = business_dates(args.start_date, args.trading_days)
    prices = make_price_records(instruments, dates, rng)
    price_batches = split_records(prices, args.price_files)

    jobs: list[dict[str, Any]] = []
    file_formats = ("csv", "json", "xlsx")
    for index, batch in enumerate(price_batches, start=1):
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"market-data-job:{args.seed}:{index}"))
        for record in batch:
            record["ingestion_job_id"] = job_id
        extension = file_formats[(index - 1) % len(file_formats)]
        filename = f"prices_batch_{index:03d}.{extension}"
        jobs.append(
            {
                "job_id": job_id,
                "created_by_email": "operator@example.test",
                "source_type": extension.upper(),
                "status": "COMPLETED",
                "requested_at": iso_timestamp(dates[0], index),
                "started_at": iso_timestamp(dates[0], index + 10),
                "completed_at": iso_timestamp(dates[-1], index + 20),
                "rows_received": len(batch),
                "rows_accepted": len(batch),
                "rows_rejected": 0,
                "source_filename": filename,
                "error_summary": "",
            }
        )
        target = args.output_dir / filename
        if extension == "csv":
            write_csv(target, batch, CANONICAL_PRICE_COLUMNS)
        elif extension == "json":
            write_json(target, batch)
        else:
            write_xlsx(target, batch, CANONICAL_PRICE_COLUMNS, "PriceRecords")

    analytics = make_analytics(prices, instruments)
    audit_events = [
        {
            "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"audit:{job['job_id']}")),
            "user_email": job["created_by_email"],
            "action": "ingestion_job.completed",
            "object_type": "IngestionJob",
            "object_id": job["job_id"],
            "request_id": f"req-{index:05d}",
            "ip_address": "127.0.0.1",
            "metadata": json.dumps({"rows_accepted": job["rows_accepted"], "source_type": job["source_type"]}),
            "created_at": job["completed_at"],
        }
        for index, job in enumerate(jobs, start=1)
    ]

    write_csv(args.output_dir / "instruments.csv", instruments, list(instruments[0].keys()))
    write_json(args.output_dir / "instruments.json", instruments)
    write_xlsx(args.output_dir / "instruments.xlsx", instruments, list(instruments[0].keys()), "Instruments")
    write_csv(args.output_dir / "ingestion_jobs.csv", jobs, list(jobs[0].keys()))
    write_csv(args.output_dir / "analytics_snapshots.csv", analytics, list(analytics[0].keys()))
    write_csv(args.output_dir / "audit_events.csv", audit_events, list(audit_events[0].keys()))
    if args.include_invalid_sample:
        generate_invalid_sample(args.output_dir / "invalid_prices_for_validation.csv")

    manifest = {
        "generator": "generate_dummy_market_data.py",
        "seed": args.seed,
        "instruments": len(instruments),
        "trading_days_per_instrument": len(dates),
        "price_records": len(prices),
        "price_batches": len(price_batches),
        "canonical_price_columns": CANONICAL_PRICE_COLUMNS,
        "note": "All values are synthetic and unsuitable for investment decisions.",
    }
    with (args.output_dir / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    print(f"Created {len(prices):,} price records across {len(price_batches)} ingestion batches in {args.output_dir}.")


if __name__ == "__main__":
    main()
