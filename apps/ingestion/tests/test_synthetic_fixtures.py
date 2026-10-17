"""The synthetic fixture generator must match the models it claims to mirror.

These tests import the models and compare their declared choices against the
values the generator writes to disk. That is the whole point: a generator whose
enum values drift from the schema produces files that look fine and fail
ingestion, which is exactly the kind of bug a quick eyeball misses.
"""

import csv
import json
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from apps.analytics.models import MetricType
from apps.ingestion.models import JobStatus, SourceType
from apps.instruments.models import AssetType
from scripts.generate_dummy_market_data import PRICE_COLUMNS, main


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def header_of(path: Path) -> list[str]:
    """Return the column names of a batch file, whatever its format."""
    if path.suffix == ".csv":
        return next(csv.reader(path.open(encoding="utf-8")))
    if path.suffix == ".json":
        return list(json.loads(path.read_text(encoding="utf-8"))[0])
    sheet = load_workbook(path, read_only=True).active
    return list(next(sheet.iter_rows(values_only=True)))


def generate(tmp_path: Path, *extra: str) -> Path:
    """Run the generator into `tmp_path` with a small, fixed shape."""
    out = tmp_path / "synthetic"
    main([
        "--output-dir", str(out),
        "--instruments", "8",
        "--trading-days", "6",
        "--price-files", "3",
        "--seed", "1234",
        *extra,
    ])
    return out


def test_it_writes_every_table_in_all_three_formats(tmp_path):
    out = generate(tmp_path)

    for table in ("exchanges", "instruments", "trading_calendar",
                  "ingestion_jobs", "ingestion_files",
                  "analytic_snapshots", "audit_events"):
        for fmt in ("csv", "json", "xlsx"):
            assert (out / "reference" / f"{table}.{fmt}").is_file(), f"{table}.{fmt}"


def test_price_batches_use_the_canonical_header(tmp_path):
    out = generate(tmp_path)

    batches = sorted((out / "batches").iterdir())
    assert len(batches) == 3
    # One batch per format, so all three readers are exercised.
    assert {batch.suffix for batch in batches} == {".csv", ".json", ".xlsx"}
    for batch in batches:
        assert header_of(batch) == PRICE_COLUMNS


def test_instrument_asset_types_are_valid_model_choices(tmp_path):
    out = generate(tmp_path)
    rows = read_csv(out / "reference" / "instruments.csv")

    written = {row["asset_type"] for row in rows}
    assert written <= set(AssetType.values)


def test_every_enum_written_is_a_declared_model_choice(tmp_path):
    out = generate(tmp_path)

    jobs = read_csv(out / "reference" / "ingestion_jobs.csv")
    assert {job["status"] for job in jobs} <= set(JobStatus.values)

    snapshots = read_csv(out / "reference" / "analytic_snapshots.csv")
    assert {s["metric_type"] for s in snapshots} <= set(MetricType.values)

    # XLSX is generated on purpose and is now a real SourceType choice, so every
    # source type the generator writes must be ingestible.
    assert {job["source_type"] for job in jobs} <= set(SourceType.values)
    assert "xlsx" in SourceType.values


def test_clean_batches_respect_the_model_check_constraints(tmp_path):
    out = generate(tmp_path)

    for batch in (out / "batches").iterdir():
        if batch.suffix != ".csv":
            continue
        for row in read_csv(batch):
            low, high = Decimal(row["low"]), Decimal(row["high"])
            open_, close = Decimal(row["open"]), Decimal(row["close"])
            assert high >= low
            assert low <= open_ <= high
            assert low <= close <= high
            assert min(open_, high, low, close) > 0
            assert int(row["volume"]) >= 0


def test_no_duplicate_instrument_and_date_across_clean_batches(tmp_path):
    out = generate(tmp_path)

    seen: set[tuple[str, str]] = set()
    for batch in (out / "batches").iterdir():
        if batch.suffix != ".csv":
            continue
        for row in read_csv(batch):
            key = (row["symbol"], row["trading_date"])
            assert key not in seen, f"duplicate {key}"
            seen.add(key)


def test_dirty_batch_contains_the_duplicate_it_claims_to(tmp_path):
    out = generate(tmp_path)
    rows = read_csv(out / "dirty" / "prices_dirty.csv")

    assert len(rows) == 15
    assert rows[0] == rows[12]  # the deliberately duplicated row


def test_missing_dates_file_omits_one_expected_trading_day(tmp_path):
    out = generate(tmp_path)
    rows = read_csv(out / "dirty" / "prices_missing_dates.csv")

    assert len(rows) == 5  # 6 trading days minus the dropped one
    assert {row["symbol"] for row in rows} == {"MSFT"}


def test_manifest_counts_match_the_files_on_disk(tmp_path):
    out = generate(tmp_path)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["instruments"] == 8
    assert manifest["trading_days"] == 6
    assert manifest["price_records"] == 8 * 6
    assert (out / "EXPECTED_RESULTS.md").is_file()


def test_the_same_seed_reproduces_identical_prices(tmp_path):
    first = generate(tmp_path / "a")
    second = generate(tmp_path / "b")

    a = (first / "batches" / "prices_batch_001.csv").read_text(encoding="utf-8")
    b = (second / "batches" / "prices_batch_001.csv").read_text(encoding="utf-8")
    assert a == b
