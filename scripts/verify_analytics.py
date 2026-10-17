"""Read-only check of the analytics engine against the real (MySQL) database.

The unit tests run on SQLite in memory, which proves the arithmetic but not the
SQL: the warm-up boundary is an offset subquery wrapped in a ``COALESCE``, and
whether MySQL executes that as written is a question only MySQL can answer. This
script reads one instrument's series and verifies, on the development database:

* the warm-up rows are returned with the requested ones, in one query;
* a ``pad`` longer than the available history still returns every requested row
  (the ``COALESCE`` fallback);
* the three calculation backends agree point for point on real data;
* ``EXPLAIN FORMAT=TREE`` shows an index range scan from the resolved boundary,
  with the scalar subqueries evaluated once rather than per row.

It writes nothing. Seed the benchmark data first if the database is empty:

    python manage.py migrate
    python scripts/bench_api.py seed

Then:

    python scripts/verify_analytics.py
    python scripts/verify_analytics.py --instrument-id 13 --rounds 50
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import connection  # noqa: E402
from django.db.models import Count, DateField, Subquery  # noqa: E402
from django.db.models.functions import Coalesce  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from apps.analytics import services  # noqa: E402
from apps.analytics.metrics import RATIO_DECIMAL_PLACES  # noqa: E402
from apps.analytics.series import load_price_series  # noqa: E402
from apps.instruments.models import Instrument  # noqa: E402
from apps.marketdata.models import PriceRecord  # noqa: E402

PAD_CASES = (1, 89)


def densest_instrument():
    """The instrument with the most prices -- the benchmark one, if seeded."""
    row = (
        PriceRecord.objects.values("instrument_id")
        .annotate(rows=Count("id"))
        .order_by("-rows")
        .first()
    )
    if row is None:
        raise SystemExit("no prices in the database; seed it first")
    return Instrument.objects.get(pk=row["instrument_id"]), row["rows"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument-id", type=int, default=None)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--window", type=int, default=31, help="requested observations")
    args = parser.parse_args()

    if connection.vendor != "mysql":
        print(f"note: running against {connection.vendor!r}, not MySQL")

    instrument = (
        Instrument.objects.get(pk=args.instrument_id)
        if args.instrument_id
        else densest_instrument()[0]
    )
    dates = list(
        PriceRecord.objects.filter(instrument=instrument)
        .order_by("trading_date")
        .values_list("trading_date", flat=True)
    )
    print(f"{instrument.symbol}: {len(dates)} rows, {dates[0]} .. {dates[-1]}")

    start = dates[max(0, len(dates) - args.window - 30)]
    end = dates[-1]
    mid = dates[len(dates) - args.window]

    print("\n-- warm-up rows --")
    for pad in PAD_CASES:
        series = load_price_series(instrument, start_date=mid, end_date=end, pad=pad)
        expected_rows = pad + len(series.in_range())
        print(
            f"pad={pad:<3} returned {len(series):>3} rows "
            f"({pad} warm-up + {len(series.in_range())} requested), "
            f"padding={series.padding}, first={series.dates[0]}"
        )
        assert series.padding == pad, f"pad={pad}: got padding={series.padding}"
        assert len(series) == expected_rows
        assert series.in_range().dates[0] == mid, "the first requested row was lost"

    # A pad longer than the history: the offset subquery finds nothing, and the
    # COALESCE must fall back to the earliest date instead of emptying the result.
    short = load_price_series(instrument, start_date=dates[0], end_date=dates[5], pad=89)
    print(
        f"pad=89 with no history: {len(short)} rows, padding={short.padding}, "
        f"{short.dates[0]} .. {short.dates[-1]}"
    )
    assert len(short) == 6 and short.padding == 0, "the COALESCE fallback dropped rows"

    print("\n-- SQL and plan --")
    boundary = Subquery(
        PriceRecord.objects.filter(instrument=instrument, trading_date__lt=mid)
        .order_by("-trading_date")
        .values("trading_date")[0:1],
        output_field=DateField(),
    )
    earliest = Subquery(
        PriceRecord.objects.filter(instrument=instrument)
        .order_by("trading_date")
        .values("trading_date")[:1],
        output_field=DateField(),
    )
    queryset = (
        PriceRecord.objects.filter(instrument=instrument, trading_date__lte=end)
        .filter(trading_date__gte=Coalesce(boundary, earliest, output_field=DateField()))
        .order_by("trading_date")
        .values_list("trading_date", "close")
    )
    sql, params = queryset.query.sql_with_params()
    print(sql.replace("`", ""))
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN FORMAT=TREE " + sql, params)
        for (line,) in cursor.fetchall():
            print("plan:", line)

    print("\n-- backends --")
    # Warm the lazy pandas/numpy import before timing anything.
    services.daily_returns_for_instrument(instrument, start_date=mid, end_date=end, backend="pandas")

    reference = None
    for backend in services.DAILY_RETURN_BACKENDS:
        samples = []
        for _ in range(args.rounds):
            started = time.perf_counter()
            result = services.daily_returns_for_instrument(
                instrument, start_date=mid, end_date=end, backend=backend
            )
            samples.append((time.perf_counter() - started) * 1000)
        if reference is None:
            reference = result.points
        assert result.points == reference, f"{backend} disagrees with the first backend"
        print(
            f"{backend:>7}: {len(result.points)} points  "
            f"median {statistics.median(samples):6.2f} ms  min {min(samples):6.2f} ms"
        )

    print(f"\nfirst three points ({RATIO_DECIMAL_PLACES} places):")
    for point in reference[:3]:
        print(
            f"  {point.trading_date}  close={point.close}  "
            f"prev={point.previous_close}  return={point.value}"
        )
    print(f"\n{reference[0].previous_close=} means the first returned date is the range start")

    with CaptureQueriesContext(connection) as captured:
        services.daily_returns_for_instrument(instrument, start_date=mid, end_date=end)
        statements = [query["sql"] for query in captured]
    print(f"queries per request: {len(statements)}")
    assert len(statements) == 1, "a request must not read in more than one query"

    print(f"\nOK (window {mid} .. {end}, {len(reference)} points)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
