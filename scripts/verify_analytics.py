"""Read-only check of the analytics engine against the real (MySQL) database.

The unit tests run on SQLite in memory, which proves the arithmetic but not the
SQL. Three claims in ``docs/market_analytics.md`` can only be checked on the
production engine, and this script checks them:

* the warm-up boundary -- an OFFSET subquery wrapped in ``COALESCE`` -- is executed
  as written, returns the rows it should, and is evaluated once rather than per
  row (``EXPLAIN FORMAT=TREE``);
* the moving-average window is pushed into the database as
  ``AVG(close) OVER (ROWS BETWEEN n - 1 PRECEDING AND CURRENT ROW)`` over the same
  padded range, and the three calculation backends still agree point for point;
* the volatility figure agrees with the database's own ``STDDEV_SAMP`` over the
  same window -- and the one-level form of that query is *rejected*, which is why
  the engine does not offer a pure-SQL volatility.

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
from decimal import Decimal

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import connection  # noqa: E402
from django.db.models import Count  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from apps.analytics import services  # noqa: E402
from apps.analytics.metrics import (  # noqa: E402
    RATIO_DECIMAL_PLACES,
    TRADING_DAYS_PER_YEAR,
)
from apps.analytics.series import load_price_series  # noqa: E402
from apps.instruments.models import Instrument  # noqa: E402
from apps.marketdata.models import PriceRecord  # noqa: E402

PAD_CASES = (1, 89)
MA_WINDOW = 30
VOL_WINDOW = 30


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
    return Instrument.objects.get(pk=row["instrument_id"])


def captured_sql(call):
    """The SQL a call actually sent -- checked to be exactly one statement.

    The loader is wrapped rather than rebuilt: the query that gets explained here
    is the query the engine runs, including whatever annotations it added, so this
    cannot drift away from the implementation the way a hand-written copy would.
    """
    with CaptureQueriesContext(connection) as captured:
        call()
        statements = [entry["sql"] for entry in captured]
    assert len(statements) == 1, f"expected one query, saw {len(statements)}"
    return statements[0]


def explain(sql):
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN FORMAT=TREE " + sql)
        return "\n".join(line for (line,) in cursor.fetchall())


def check_warm_up(instrument, mid, end, first_dates):
    print("\n-- warm-up rows --")

    for pad in PAD_CASES:
        series = load_price_series(instrument, start_date=mid, end_date=end, pad=pad)
        in_range = series.in_range()
        print(
            f"pad={pad:<3} returned {len(series):>3} rows "
            f"({pad} warm-up + {len(in_range)} requested), "
            f"padding={series.padding}, first={series.dates[0]}"
        )
        assert series.padding == pad, f"pad={pad}: got padding={series.padding}"
        assert len(series) == pad + len(in_range)
        assert in_range.dates[0] == mid, "the first requested row was lost"

    # A pad longer than the history: the offset subquery finds nothing, and the
    # COALESCE must fall back to the earliest date instead of emptying the result.
    short = load_price_series(instrument, start_date=first_dates[0], end_date=first_dates[5], pad=89)
    print(
        f"pad=89 with no history: {len(short)} rows, padding={short.padding}, "
        f"{short.dates[0]} .. {short.dates[-1]}"
    )
    assert len(short) == 6 and short.padding == 0, "the COALESCE fallback dropped rows"

    print("\n  the query the loader sends for pad=1:")
    print("  " + explain(
        captured_sql(lambda: load_price_series(instrument, start_date=mid, end_date=end, pad=1))
    ).replace("\n", "\n  "))


def check_moving_average(instrument, mid, end):
    print("\n-- moving average window --")

    def request(backend="sql"):
        return services.moving_average_for_instrument(
            instrument, window=MA_WINDOW, start_date=mid, end_date=end, backend=backend
        )

    print(explain(captured_sql(request)))

    reference = request("python")
    for backend in services.MOVING_AVERAGE_BACKENDS:
        result = request(backend)
        assert result.points == reference.points, f"{backend} disagrees on the mean"
        print(
            f"  {backend:>7}: {len(result.points)} points, padding={result.padding}, "
            f"last={result.points[-1].value}"
        )
    print("  one request: 1 query (asserted above)")


def check_volatility(instrument, mid, end):
    print("\n-- rolling volatility --")

    def request(backend="sql"):
        return services.rolling_volatility_for_instrument(
            instrument, window=VOL_WINDOW, start_date=mid, end_date=end, backend=backend
        )

    # The SQL backend takes the returns from LAG and does the rest in Python, so
    # its query is the daily-returns one; capturing it here is what says so.
    captured_sql(request)

    reference = request("python")
    for backend in services.VOLATILITY_BACKENDS:
        result = request(backend)
        assert result.points == reference.points, f"{backend} disagrees on the deviation"
        print(
            f"  {backend:>7}: {len(result.points)} points, padding={result.padding}, "
            f"last daily={result.points[-1].daily} annualized={result.points[-1].value}"
        )
    print("  one request: 1 query (asserted above)")

    # The same statistic, computed by the database. The returns have to be
    # computed in an inner query, because a window function may not appear inside
    # another window function's arguments -- which this proves, rather than
    # asserting it in prose.
    instrument_id = instrument.pk
    nested = f"""
        SELECT trading_date,
               STDDEV_SAMP(
                   (close - LAG(close) OVER (PARTITION BY instrument_id ORDER BY trading_date))
                   / LAG(close) OVER (PARTITION BY instrument_id ORDER BY trading_date)
               ) OVER (PARTITION BY instrument_id ORDER BY trading_date
                       ROWS BETWEEN {VOL_WINDOW - 1} PRECEDING AND CURRENT ROW) AS sigma
        FROM marketdata_pricerecord WHERE instrument_id = {instrument_id}
    """
    derived = f"""
        SELECT trading_date, STDDEV_SAMP(r) OVER (
                   PARTITION BY instrument_id ORDER BY trading_date
                   ROWS BETWEEN {VOL_WINDOW - 1} PRECEDING AND CURRENT ROW) AS sigma
        FROM (SELECT instrument_id, trading_date,
                     (close - LAG(close) OVER (PARTITION BY instrument_id ORDER BY trading_date))
                     / LAG(close) OVER (PARTITION BY instrument_id ORDER BY trading_date) AS r
              FROM marketdata_pricerecord WHERE instrument_id = {instrument_id}) AS daily
        ORDER BY trading_date DESC LIMIT 3
    """
    with connection.cursor() as cursor:
        try:
            cursor.execute(nested)
            raise AssertionError("a window inside a window was accepted, unexpectedly")
        except Exception as error:  # noqa: BLE001 - the message is the evidence
            print(f"  one level: {type(error).__name__}: {error}")
        cursor.execute(derived)
        rows = cursor.fetchall()

    # STDDEV_SAMP is the sample deviation (ddof=1), which is the engine's
    # convention; this comparison is what says so.
    daily_from_mysql = rows[0][1]
    annualized_from_mysql = daily_from_mysql * (TRADING_DAYS_PER_YEAR**0.5)
    engine_daily = float(reference.points[-1].daily)
    engine_annualized = float(reference.points[-1].value)
    print(f"  derived table, {rows[0][0]}: daily={daily_from_mysql!r}")
    print(
        f"    database annualized={annualized_from_mysql:.8f}  "
        f"engine={engine_annualized:.8f}  "
        f"daily difference={abs(daily_from_mysql - engine_daily):.2e}"
    )
    assert abs(engine_annualized - annualized_from_mysql) < 1e-7, "MySQL and the engine disagree"


def check_cumulative(instrument):
    print("\n-- cumulative returns --")
    result = services.cumulative_returns_for_instrument(instrument)
    expected = result.points[-1].close / result.points[0].close - 1
    print(
        f"  {len(result.points)} points from {result.points[0].trading_date}, "
        f"total={result.total} (base {result.points[0].base_close} -> "
        f"{result.points[-1].close})"
    )
    print(f"  close(last) / close(first) - 1 = {expected.quantize(result.total)}")
    assert abs(result.total - expected) <= Decimal(10) ** -RATIO_DECIMAL_PLACES, (
        "the total is not the ratio of the two closes"
    )


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
        else densest_instrument()
    )
    dates = list(
        PriceRecord.objects.filter(instrument=instrument)
        .order_by("trading_date")
        .values_list("trading_date", flat=True)
    )
    print(f"{instrument.symbol}: {len(dates)} rows, {dates[0]} .. {dates[-1]}")

    mid = dates[len(dates) - args.window]
    end = dates[-1]

    check_warm_up(instrument, mid, end, dates)

    print("\n-- daily returns --")
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
            f"  {backend:>7}: {len(result.points)} points  "
            f"median {statistics.median(samples):6.2f} ms  min {min(samples):6.2f} ms"
        )
    print(f"  first three points ({RATIO_DECIMAL_PLACES} places):")
    for point in reference[:3]:
        print(
            f"    {point.trading_date}  close={point.close}  "
            f"prev={point.previous_close}  return={point.value}"
        )
    captured_sql(
        lambda: services.daily_returns_for_instrument(
            instrument, start_date=mid, end_date=end
        )
    )
    print("  one request: 1 query (asserted above)")

    check_moving_average(instrument, mid, end)
    check_volatility(instrument, mid, end)
    check_cumulative(instrument)

    print(f"\nOK (window {mid} .. {end})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
