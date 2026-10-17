"""Read-only check of the analytics engine against the real (MySQL) database.

The unit tests run on SQLite in memory, which proves the arithmetic but not the
SQL. The claims in ``docs/market_analytics.md`` that can only be checked on the
production engine are checked here:

* the warm-up boundary -- an OFFSET subquery wrapped in ``COALESCE`` -- is executed
  as written, returns the rows it should, and is evaluated once rather than per
  row (``EXPLAIN FORMAT=TREE``);
* the moving-average window is pushed into the database as
  ``AVG(close) OVER (ROWS BETWEEN n - 1 PRECEDING AND CURRENT ROW)`` over the same
  padded range, and the three calculation backends still agree point for point;
* the volatility figure agrees with the database's own ``STDDEV_SAMP`` over the
  same window -- and the one-level form of that query is *rejected*, which is why
  the engine does not offer a pure-SQL volatility;
* the ranking reads the whole universe in one query, and the row-number form it
  declines selects the same rows *slower*, which is the measurement behind
  ``RANKING_BACKENDS`` having no ``sql`` member.

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
    RANKING_MEASURES,
    RATIO_DECIMAL_PLACES,
    TRADING_DAYS_PER_YEAR,
    SelectedPeriod,
)
from apps.analytics.series import (  # noqa: E402
    load_price_cross_section,
    load_price_series,
)
from apps.instruments.models import Instrument  # noqa: E402
from apps.marketdata.models import PriceRecord  # noqa: E402

PAD_CASES = (1, 89)
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
    print("\n-- moving averages (three named series, one read) --")

    def request(backend="sql"):
        return services.moving_averages_for_instrument(
            instrument, start_date=mid, end_date=end, backend=backend
        )

    print(explain(captured_sql(request)))

    reference = request("python")
    for backend in services.MOVING_AVERAGE_BACKENDS:
        result = request(backend)
        for left, right in zip(result.series, reference.series):
            assert left.points == right.points, (
                f"{backend} disagrees on {left.name}"
            )
        print(
            f"  {backend:>7}: {len(result.series)} series {result.windows}, "
            f"padding={result.padding}, undefined={result.undefined}"
        )

    for named in reference.series:
        print(
            f"    {named.name:<18} window={named.window:<3} "
            f"last={named.points[-1].value}"
        )
    print("  one request for all three: 1 query (asserted above)")

    # The three windows are one read: the SQL carries all three frames, which the
    # plan above shows as three window aggregates over one sorted buffer.
    sql = captured_sql(request)
    for window in reference.windows:
        assert f"ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW" in sql, (
            f"the window for {window} is missing from the query"
        )
    print("  all three frames are in the one statement")


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


def check_drawdown(instrument, mid, end):
    print("\n-- maximum drawdown --")

    def request(backend="sql"):
        return services.maximum_drawdown_for_instrument(
            instrument, start_date=mid, end_date=end, backend=backend
        )

    print(explain(captured_sql(request)))

    reference = request("python")
    for backend in services.DRAWDOWN_BACKENDS:
        result = request(backend)
        assert result.points == reference.points, f"{backend} disagrees on the drawdown"
        assert result.summary == reference.summary, f"{backend} disagrees on the episode"
        print(
            f"  {backend:>7}: {len(result.points)} points, padding={result.padding}, "
            f"max {result.summary.value} from {result.summary.peak_date} "
            f"to {result.summary.trough_date}, recovered={result.summary.recovered}"
        )
    print("  one request: 1 query (asserted above)")

    # The same statistic, written by hand against the database: the running maximum
    # in an inner query, the worst ratio outside it. The nesting is what the derived
    # table is for -- a window function cannot be an argument to another window
    # function, which the first statement proves rather than asserts.
    instrument_id = instrument.pk
    nested = f"""
        SELECT MIN(close / MAX(close) OVER (ORDER BY trading_date
                                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - 1)
               OVER (ORDER BY trading_date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
        FROM marketdata_pricerecord WHERE instrument_id = {instrument_id}
    """
    derived = f"""
        SELECT MIN(peak.ratio) FROM (
            SELECT close / MAX(close) OVER (ORDER BY trading_date
                                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - 1
                   AS ratio
            FROM marketdata_pricerecord
            WHERE instrument_id = {instrument_id}
              AND trading_date BETWEEN '{mid}' AND '{end}'
        ) AS peak
    """
    with connection.cursor() as cursor:
        try:
            cursor.execute(nested)
            raise AssertionError("a window inside a window was accepted, unexpectedly")
        except Exception as error:  # noqa: BLE001 - the message is the evidence
            print(f"  one level: {type(error).__name__}: {error}")
        cursor.execute(derived)
        worst_raw = cursor.fetchone()[0]

    worst_from_mysql = float(worst_raw)
    engine_worst = float(reference.summary.value)
    print(f"  derived table: worst ratio={worst_raw}")
    print(
        f"    database={worst_from_mysql:.8f}  engine={engine_worst:.8f}  "
        f"difference={abs(worst_from_mysql - engine_worst):.2e}"
    )
    assert abs(engine_worst - worst_from_mysql) < 1e-7, "MySQL and the engine disagree"


def check_extremes(instrument, mid, end):
    print("\n-- price extremes --")

    def request(backend="sql"):
        return services.price_extremes_for_instrument(
            instrument, start_date=mid, end_date=end, backend=backend
        )

    print(explain(captured_sql(request)))

    reference = request("python")
    for backend in services.EXTREME_BACKENDS:
        result = request(backend)
        assert result.points == reference.points, (
            f"{backend} disagrees on the running extremes"
        )
        assert result.summary == reference.summary, f"{backend} disagrees on the summary"

    summary = reference.summary
    print(
        f"  {len(reference.points)} points, padding={reference.padding}, "
        f"undefined={reference.undefined}"
    )
    print(
        f"  closing:  high {summary.highest_close} on {summary.highest_close_date}, "
        f"low {summary.lowest_close} on {summary.lowest_close_date}"
    )
    print(
        f"  traded:   high {summary.highest_high} on {summary.highest_high_date}, "
        f"low {summary.lowest_low} on {summary.lowest_low_date}, "
        f"range {summary.traded_range}"
    )
    print("  one request: 1 query (asserted above)")

    # The database's own aggregates, from the same columns the metric reads. This is
    # the closest thing to a cross-check for an extreme: the *date* it happened is the
    # part SQL cannot return, which is why the engine keeps the rows.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT MAX(close), MIN(close), MAX(high), MIN(low) "
            "FROM marketdata_pricerecord "
            f"WHERE instrument_id = {instrument.pk} "
            f"AND trading_date BETWEEN '{mid}' AND '{end}'"
        )
        db_high_close, db_low_close, db_high_high, db_low_low = cursor.fetchone()

    print(
        f"  MySQL: MAX(close)={db_high_close} MIN(close)={db_low_close} "
        f"MAX(high)={db_high_high} MIN(low)={db_low_low}"
    )
    assert Decimal(db_high_close) == summary.highest_close
    assert Decimal(db_low_close) == summary.lowest_close
    assert Decimal(db_high_high) == summary.highest_high
    assert Decimal(db_low_low) == summary.lowest_low
    print("  the engine's four extremes equal the database's four aggregates")


def check_volume(instrument, mid, end):
    print("\n-- volume summaries --")

    def request(backend="sql"):
        return services.volume_summaries_for_instrument(
            instrument, start_date=mid, end_date=end, backend=backend
        )

    print(explain(captured_sql(request)))

    reference = request("python")
    for backend in services.VOLUME_BACKENDS:
        result = request(backend)
        assert result.points == reference.points, (
            f"{backend} disagrees on the running totals"
        )
        assert result.summary == reference.summary, f"{backend} disagrees on the summary"

    summary = reference.summary
    print(
        f"  {len(reference.points)} points, padding={reference.padding}, "
        f"undefined={reference.undefined}"
    )
    print(
        f"  total {summary.total_volume}, average {summary.average_volume} per session, "
        f"{summary.observations} sessions"
    )
    print(
        f"  heaviest {summary.highest_volume} on {summary.highest_volume_date}, "
        f"lightest {summary.lowest_volume} on {summary.lowest_volume_date}"
    )
    print(
        f"  above {summary.sessions_above_average}, at {summary.sessions_at_average}, "
        f"below {summary.sessions_below_average}, "
        f"zero-volume sessions {summary.zero_volume_sessions}"
    )
    print("  one request: 1 query (asserted above)")

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT SUM(volume), AVG(volume), MAX(volume), MIN(volume), COUNT(*) "
            "FROM marketdata_pricerecord "
            f"WHERE instrument_id = {instrument.pk} "
            f"AND trading_date BETWEEN '{mid}' AND '{end}'"
        )
        db_total, db_average, db_heaviest, db_lightest, db_sessions = cursor.fetchone()

    print(
        f"  MySQL: SUM={db_total} AVG={db_average} MAX={db_heaviest} "
        f"MIN={db_lightest} COUNT={db_sessions}"
    )
    assert Decimal(db_total) == summary.total_volume
    assert Decimal(db_heaviest) == summary.highest_volume
    assert Decimal(db_lightest) == summary.lowest_volume
    assert db_sessions == summary.observations
    # The engine rounds the exact quotient to a count; MySQL's AVG is already a
    # decimal, so the two can differ by the last place of a rounding. The exact
    # figures (SUM and COUNT) are asserted above.
    assert abs(Decimal(db_average) - summary.average_volume) <= 1, (
        "the engine's average is not the database's"
    )
    print("  the engine's total, extremes and session count are the database's")

    # Trading days versus calendar days, on the same total: the paragraph in the
    # documentation, computed rather than asserted.
    span = (end - mid).days + 1
    per_calendar_day = Decimal(db_total) / span
    line = (
        f"  {summary.observations} sessions span {span} calendar days: "
        f"per session {summary.average_volume}, per calendar day {per_calendar_day:.0f}"
    )
    if summary.average_volume:
        line += f" ({(1 - per_calendar_day / summary.average_volume) * 100:.0f}% lower)"
    print(line)


def check_percentage_change(instrument, mid, end, dates):
    print("\n-- percentage change over a selected period --")

    def request(**kwargs):
        return services.percentage_change_for_instrument(
            instrument, start_date=mid, end_date=end, **kwargs
        )

    captured_sql(request)
    result = request()
    period = result.periods[0]
    print(
        f"  {period.base_date} {period.base_close} -> {period.end_date} "
        f"{period.end_close}: change={period.change} percent={period.percent_change} "
        f"over {period.observations} sessions"
    )

    cumulative = services.cumulative_returns_for_instrument(
        instrument, start_date=mid, end_date=end
    )
    print(f"  the cumulative-return series over the same range ends on {cumulative.total}")
    assert period.change == cumulative.total, "the two ways of seeing it disagree"

    for backend in services.PERIOD_CHANGE_BACKENDS:
        other = request(backend=backend)
        assert other.periods == result.periods, f"{backend} disagrees"
    print("  python and pandas agree; one period is 1 query (asserted above)")

    # Several periods at once: one read over their union, and one number each.
    periods = (
        SelectedPeriod(label="last 5", start_date=dates[-5], end_date=end),
        SelectedPeriod(label="last 21", start_date=dates[-21], end_date=end),
        SelectedPeriod(label="window", start_date=mid, end_date=end),
        SelectedPeriod(label="to date", end_date=end),
    )
    captured_sql(
        lambda: services.percentage_change_for_instrument(instrument, periods=periods)
    )
    many = services.percentage_change_for_instrument(instrument, periods=periods)
    print(
        f"  four periods, read over {many.start_date} .. {many.end_date}, in 1 query:"
    )
    for change in many.periods:
        print(
            f"    {change.label:<8} {change.base_date} -> {change.end_date}  "
            f"change={change.change}  percent={change.percent_change}"
        )

    # The SQL form that is deliberately not used, run to show that it agrees: two
    # index lookups rather than a read of the period.
    endpoint_sql = (
        "SELECT "
        f"(SELECT close FROM marketdata_pricerecord WHERE instrument_id = {instrument.pk} "
        f"AND trading_date >= '{mid}' ORDER BY trading_date ASC LIMIT 1), "
        f"(SELECT close FROM marketdata_pricerecord WHERE instrument_id = {instrument.pk} "
        f"AND trading_date <= '{end}' ORDER BY trading_date DESC LIMIT 1)"
    )
    print(explain(endpoint_sql))
    with connection.cursor() as cursor:
        cursor.execute(endpoint_sql)
        base_close, end_close = cursor.fetchone()

    from_mysql = (Decimal(end_close) - Decimal(base_close)) / Decimal(base_close)
    print(
        f"  two index lookups: {base_close} -> {end_close} = {float(from_mysql):.10f}, "
        f"engine {period.change}"
    )
    assert abs(float(period.change) - float(from_mysql)) < 1e-8, (
        "the engine and the database's own endpoints disagree"
    )
    print("  and the endpoint query is two index lookups, not a scan of the period")


def check_ranking(mid, end, window, rounds):
    print("\n-- rankings across instruments --")

    def request(measure, backend="python"):
        return services.ranking_for_instruments(
            measure=measure,
            backend=backend,
            lookback=window,
            start_date=mid,
            end_date=end,
        )

    def time_call(call):
        samples = []
        for _ in range(rounds):
            started = time.perf_counter()
            call()
            samples.append((time.perf_counter() - started) * 1000)
        return statistics.median(samples)

    def raw(sql):
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchall()

    results = {}
    for measure in RANKING_MEASURES:
        reference = request(measure)
        results[measure] = reference
        for backend in services.RANKING_BACKENDS:
            result = request(measure, backend)
            assert result.rows == reference.rows, (
                f"{backend} disagrees on the {measure} ranking"
            )

        print(
            f"  {measure:>6}: universe {reference.universe}, ranked {reference.ranked}, "
            f"unranked {reference.unranked}, lookback {reference.lookback} sessions"
        )
        for row in reference.rows[:5]:
            print(
                f"    {row.rank:>4} {row.symbol:<10} {row.value:>18} "
                f"obs={row.observations} {row.first_date} .. {row.last_date}"
            )
        last = reference.rows[-1]
        print(f"    {last.rank:>4} {last.symbol:<10} {last.value:>18}")

    for backend in services.RANKING_BACKENDS:
        captured_sql(lambda backend=backend: request("return", backend))
    print("  one request: 1 query (asserted above)")

    for measure in RANKING_MEASURES:
        for backend in services.RANKING_BACKENDS:
            median = time_call(
                lambda measure=measure, backend=backend: request(measure, backend)
            )
            print(f"  {measure:>6} {backend:>7}: median {median:6.2f} ms")

    # The universe is a fact about the prices in the range, not about the
    # instrument table: this is the count the metric claims to be counting.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(DISTINCT instrument_id) FROM marketdata_pricerecord "
            f"WHERE trading_date BETWEEN '{mid}' AND '{end}'"
        )
        db_instruments = cursor.fetchone()[0]
    print(f"  MySQL: {db_instruments} instruments have prices between {mid} and {end}")
    assert results["return"].universe == db_instruments

    # Every row's measure is the per-instrument metric the engine already has:
    # metric 8 for a return, metric 7 for a volume. This is what keeps a ranking
    # from being a second, subtly different definition of the same number.
    top_return = results["return"].rows[0]
    change = services.percentage_change_for_instrument(
        Instrument.objects.get(pk=top_return.instrument_id),
        start_date=top_return.first_date,
        end_date=top_return.last_date,
    ).periods[0]
    print(
        f"  best return: {top_return.symbol} {top_return.value} over "
        f"{top_return.observations} sessions; metric 8 over those two dates gives "
        f"{change.change}"
    )
    assert top_return.value == change.change

    top_volume = results["volume"].rows[0]
    summary = services.volume_summaries_for_instrument(
        Instrument.objects.get(pk=top_volume.instrument_id),
        start_date=mid,
        end_date=end,
    ).summary
    print(
        f"  heaviest:    {top_volume.symbol} {top_volume.value} over "
        f"{top_volume.observations} sessions; metric 7 over the same range totals "
        f"{summary.total_volume}"
    )
    assert top_volume.observations == window
    assert top_volume.value == summary.total_volume

    print("\n  the read the ranking uses (one range query for the universe):")
    cross_sql = captured_sql(
        lambda: load_price_cross_section(start_date=mid, end_date=end)
    )
    print("  " + explain(cross_sql).replace("\n", "\n  "))

    # The SQL form RANKING_BACKENDS declines, run for the measurement the note in
    # ``services`` cites -- in both shapes, because the trade-off turns on whether
    # the range bounds the read. It is legal (a partitioned row number in a derived
    # table) and it selects exactly the rows the metric windows to.
    def row_number_sql(bounds):
        where = f"WHERE trading_date BETWEEN '{mid}' AND '{end}'" if bounds else ""
        window_function = (
            "ROW_NUMBER() OVER (PARTITION BY instrument_id "
            "ORDER BY trading_date DESC) AS rn"
        )
        return (
            "SELECT instrument_id, trading_date, close FROM ("
            f"SELECT instrument_id, trading_date, close, {window_function} "
            f"FROM marketdata_pricerecord {where}"
            f") AS ranked WHERE rn <= {window}"
        )

    def compare_reads(bounds, label):
        sql = row_number_sql(bounds)
        kwargs = {"start_date": mid, "end_date": end} if bounds else {}
        plain = load_price_cross_section(**kwargs)
        plain_rows = sum(len(series) for series in plain.series.values())
        windowed = {(row[0], row[1], Decimal(row[2])) for row in raw(sql)}
        expected = {
            (instrument_id, trading_date, Decimal(close))
            for instrument_id, series in plain.series.items()
            for trading_date, close in zip(
                series.tail(window).dates, series.tail(window).closes
            )
        }
        plain_ms = time_call(lambda: load_price_cross_section(**kwargs))
        windowed_ms = time_call(lambda: raw(sql))
        print(
            f"    {label:<9} range read {plain_ms:7.2f} ms / {plain_rows:>6} rows    "
            f"row-number {windowed_ms:7.2f} ms / {len(windowed):>6} rows"
        )
        assert windowed == expected, f"the row-number read returns other rows ({label})"

    print("\n  the read, and the form that RANKING_BACKENDS declines for it:")
    compare_reads(True, "bounded")
    compare_reads(False, "unbounded")
    print("  both select the same rows; the window has to be materialised before the")
    print("  filter can apply, which is what the row-number form costs:")
    print("  " + explain(row_number_sql(True)).replace("\n", "\n  "))


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
    check_drawdown(instrument, mid, end)
    check_extremes(instrument, mid, end)
    check_volume(instrument, mid, end)
    check_percentage_change(instrument, mid, end, dates)
    check_ranking(mid, end, args.window, args.rounds)

    print(f"\nOK (window {mid} .. {end})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
