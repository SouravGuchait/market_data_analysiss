"""Business logic for analytics: what the API calls, and what it calls into.

Two halves. The first is snapshot reads, which are stored numbers. The second is
the metric entry points, which turn a request -- one instrument, a date range, a
backend -- into a calculation. The entry points are thin on purpose: they choose
a loader and a calculation and hand back the pair, so the decisions a reader
needs to audit (which rows were read, which arithmetic was applied) are visible
in one place instead of spread through a view.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from .metrics import (
    TRADING_DAYS_PER_YEAR,
    CumulativePoint,
    DrawdownPoint,
    DrawdownSummary,
    MovingAveragePoint,
    ReturnPoint,
    VolatilityPoint,
    cumulative_returns,
    cumulative_returns_pandas,
    daily_returns,
    daily_returns_pandas,
    drawdown_series,
    drawdown_series_pandas,
    moving_average,
    moving_average_pandas,
    rolling_volatility,
    rolling_volatility_pandas,
    summarize_drawdown,
)
from .models import AnalyticSnapshot
from .series import load_price_series


def get_snapshot_queryset():
    """The base queryset for every analytics read.

    ``select_related`` joins the instrument, because every representation shows
    which symbol a metric belongs to.
    """
    return AnalyticSnapshot.objects.select_related("instrument")


def list_snapshots_for_instrument(
    instrument,
    *,
    metric_type=None,
    start_date=None,
    end_date=None,
    lookback_days=None,
):
    """Returns snapshots for one instrument, newest date first.

    All arguments are optional and additive.
    """
    queryset = get_snapshot_queryset().filter(instrument=instrument)

    if metric_type:
        queryset = queryset.filter(metric_type=metric_type)
    if lookback_days is not None:
        queryset = queryset.filter(lookback_days=lookback_days)
    if start_date:
        queryset = queryset.filter(as_of_date__gte=start_date)
    if end_date:
        queryset = queryset.filter(as_of_date__lte=end_date)

    return queryset.order_by("-as_of_date", "metric_type")


def get_latest_snapshot(instrument, metric_type, *, lookback_days=None):
    """Returns the most recent snapshot of one metric, or None.

    Relies on ``(instrument, metric_type, as_of_date)`` being indexed, so this is
    an index lookup rather than a scan and sort.
    """
    queryset = get_snapshot_queryset().filter(
        instrument=instrument, metric_type=metric_type
    )
    if lookback_days is not None:
        queryset = queryset.filter(lookback_days=lookback_days)

    return queryset.order_by("-as_of_date").first()


# --------------------------------------------------------------------------- #
# Metric results and entry points
# --------------------------------------------------------------------------- #

#: The interchangeable calculation backends.
#:
#: * ``sql``    -- the window comes from the database: ``LAG(close)`` for the
#:                 previous session, ``AVG(close) OVER (...)`` for a trailing
#:                 mean;
#: * ``python`` -- the window is computed over a list already in memory;
#: * ``pandas`` -- the window is a vectorised ``shift`` / ``rolling``.
#:
#: Every backend produces the same points, and the tests assert it row for row.
#: ``sql`` is the default wherever it exists because the engine's canonical path
#: keeps the window in the database -- with the honest limit stated per metric:
#: for one small instrument the rows are in memory either way, so the window is
#: the same work and the SQL path even returns an extra column. ``LAG`` and
#: ``AVG() OVER`` earn their place as the series or the read shape grows, when the
#: windowed value is consumed in the database (a rank, a filter on the level) or
#: when the alternative would be shipping every row to Python to compute a window.
#:
#: Two metric families have no ``sql`` member, and both for the same reason -- the
#: window would have to be computed *from other windowed values*, which SQL does
#: not allow:
#:
#: * cumulative returns need a product, and no SQL dialect has a windowed
#:   product. ``EXP(SUM(LN(1 + r)))`` is the usual workaround, and it is both
#:   numerically unstable and undefined for ``r <= -100%``;
#: * rolling volatility needs the standard deviation *of the returns*, and the
#:   returns themselves are ``LAG(close)``; a window function may not appear
#:   inside another window function's arguments.
#:
#: Both are computed over rows the loader has already returned, which costs one
#: query either way.
#:
#: Maximum drawdown *does* have a SQL member, which is the test of that rule
#: rather than an exception to it: its window -- the running maximum -- is a
#: plain frame over the closes, so ``MAX(close) OVER (ROWS BETWEEN UNBOUNDED
#: PRECEDING AND CURRENT ROW)`` is one legal window, and only the ratio on top of
#: it is arithmetic.
DAILY_RETURN_BACKENDS = ("sql", "python", "pandas")
CUMULATIVE_RETURN_BACKENDS = ("python", "pandas")
MOVING_AVERAGE_BACKENDS = ("sql", "python", "pandas")
VOLATILITY_BACKENDS = ("sql", "python", "pandas")
DRAWDOWN_BACKENDS = ("sql", "python", "pandas")


@dataclass(frozen=True)
class MetricResult:
    """What every metric entry point returns, beyond its own point type.

    The shared part is small and deliberate: which instrument, which backend
    produced the arithmetic, which range was answered, how many leading rows were
    warm-up, and the points themselves. ``undefined`` is defined once here rather
    than four times, because "how many values are missing" must not mean something
    slightly different per metric.
    """

    symbol: str
    backend: str
    start_date: date | None = None
    end_date: date | None = None
    padding: int = 0
    points: list = field(default_factory=list)

    @property
    def undefined(self) -> int:
        """How many points carry no value.

        Reported rather than dropped: a hole in a series is a fact about the data
        (the first day, a zero denominator, a window that has not filled yet), and
        a count is how a caller notices it without inspecting every row.
        """
        return sum(1 for point in self.points if point.value is None)


@dataclass(frozen=True)
class DailyReturns(MetricResult):
    """One daily-returns answer, with the inputs that produced it.

    ``points`` covers the requested range only: the warm-up row the loader read
    is accounted for by ``padding`` rather than returned as if it were an answer.
    """

    points: list[ReturnPoint] = field(default_factory=list)


@dataclass(frozen=True)
class CumulativeReturns(MetricResult):
    """One cumulative-returns answer.

    ``total`` is the number a report quotes: the range's return by its last
    observation. It is ``None`` when the chain broke before the end, because the
    total is then genuinely unknown -- and a value computed from part of the range
    would be worse than no value at all.
    """

    points: list[CumulativePoint] = field(default_factory=list)

    @property
    def total(self):
        return self.points[-1].value if self.points else None


@dataclass(frozen=True)
class MovingAverages(MetricResult):
    """One moving-average answer, and the window it was asked for."""

    window: int = 0
    points: list[MovingAveragePoint] = field(default_factory=list)


@dataclass(frozen=True)
class RollingVolatility(MetricResult):
    """One volatility answer, and the conventions it was computed under."""

    window: int = 0
    periods_per_year: int = 0
    points: list[VolatilityPoint] = field(default_factory=list)


@dataclass(frozen=True)
class MaximumDrawdown(MetricResult):
    """One drawdown answer: the per-date decline, and the worst episode in it.

    The summary is a field rather than seven more attributes on the result, so
    that the flat part of the result stays the shared part and the episode stays
    one object with its own docstring.
    """

    summary: DrawdownSummary = field(default_factory=DrawdownSummary)
    points: list[DrawdownPoint] = field(default_factory=list)


def _reject_backend(backend: str, allowed: Sequence[str]) -> None:
    """Fails loudly on a backend name that is not interchangeable."""
    if backend not in allowed:
        raise ValueError(
            f"unknown backend {backend!r}; expected one of {', '.join(allowed)}"
        )


def daily_returns_for_instrument(
    instrument,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    backend: str = "sql",
) -> DailyReturns:
    """Daily simple returns for one instrument over a date range.

    The range is inclusive and bounds *observations*, not calendar days. The row
    before ``start_date`` is read -- and only that row, ``pad=1`` -- so the first
    requested date has a return instead of a hole. Without it every response
    would begin with ``null``, because the window function and the Python shift
    alike can only see the rows the query returned.

    One query, whatever the backend: the choice of arithmetic does not change how
    the data is fetched, which is what keeps the backends comparable.
    """
    _reject_backend(backend, DAILY_RETURN_BACKENDS)

    series = load_price_series(
        instrument,
        start_date=start_date,
        end_date=end_date,
        pad=1,
        with_previous_close=backend == "sql",
    )

    if backend == "sql":
        points = daily_returns(
            series.dates, series.closes, previous_closes=series.column("previous_close")
        )
    elif backend == "pandas":
        points = daily_returns_pandas(series.dates, series.closes)
    else:
        points = daily_returns(series.dates, series.closes)

    return DailyReturns(
        symbol=series.symbol,
        backend=backend,
        start_date=start_date,
        end_date=end_date,
        padding=series.padding,
        points=points[series.padding :],
    )


def cumulative_returns_for_instrument(
    instrument,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    backend: str = "python",
) -> CumulativeReturns:
    """The range's compounded return up to each observation, for one instrument.

    No warm-up row is read, and that is the difference between this metric and
    the daily returns above: the base of the accumulation is the **first
    observation inside the range**, whose cumulative return is ``0``. Reading the
    row before it would move the base outside the range and turn the first answer
    into the move *into* the period -- a different question, answered by the daily
    return of that date. So ``pad=0``, and ``padding`` is always ``0`` here.

    Because the base is inside the range, the total the range reports
    (``result.total``) is ``close(last) / close(first) - 1``: the period's return,
    computed from the period's own prices.

    ``python`` is the default because there is no SQL backend to default to: SQL
    has no windowed product -- see the note on
    :data:`CUMULATIVE_RETURN_BACKENDS`.
    """
    _reject_backend(backend, CUMULATIVE_RETURN_BACKENDS)

    series = load_price_series(instrument, start_date=start_date, end_date=end_date)

    if backend == "pandas":
        points = cumulative_returns_pandas(series.dates, series.closes)
    else:
        points = cumulative_returns(series.dates, series.closes)

    return CumulativeReturns(
        symbol=series.symbol,
        backend=backend,
        start_date=start_date,
        end_date=end_date,
        padding=series.padding,
        points=points,
    )


def moving_average_for_instrument(
    instrument,
    *,
    window: int,
    start_date: date | None = None,
    end_date: date | None = None,
    backend: str = "sql",
) -> MovingAverages:
    """The trailing mean of ``window`` observations, for one instrument.

    The loader reads ``window - 1`` observations before ``start_date``, so the
    first requested date has a full window rather than a ``null`` that the
    database could have answered. This is the metric that makes the loader's
    general ``pad`` worth having: one row of warm-up was enough for a return, a
    90-day average needs 89.

    In the ``sql`` backend the mean itself comes from
    ``AVG(close) OVER (ROWS BETWEEN window - 1 PRECEDING AND CURRENT ROW)``, so
    the number is computed where the rows are; the other two compute it in Python
    over the same rows. All three agree, including on the partial windows at the
    start of a history shorter than the lookback, because the completeness rule
    (``sample_size == window``) is applied identically to a supplied mean.
    """
    _reject_backend(backend, MOVING_AVERAGE_BACKENDS)
    if window < 1:
        raise ValueError("window must be at least 1")

    series = load_price_series(
        instrument,
        start_date=start_date,
        end_date=end_date,
        pad=window - 1,
        moving_average_window=window if backend == "sql" else None,
    )

    if backend == "sql":
        points = moving_average(
            series.dates,
            series.closes,
            window,
            values=series.column("moving_average"),
        )
    elif backend == "pandas":
        points = moving_average_pandas(series.dates, series.closes, window)
    else:
        points = moving_average(series.dates, series.closes, window)

    return MovingAverages(
        symbol=series.symbol,
        backend=backend,
        start_date=start_date,
        end_date=end_date,
        padding=series.padding,
        window=window,
        points=points[series.padding :],
    )


def rolling_volatility_for_instrument(
    instrument,
    *,
    window: int,
    start_date: date | None = None,
    end_date: date | None = None,
    backend: str = "sql",
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> RollingVolatility:
    """The annualized standard deviation of the last ``window`` returns.

    ``window`` counts returns here, not observations, which is the convention for
    this statistic ("20-day volatility" means twenty daily returns). It therefore
    needs ``window + 1`` closes, and the loader reads ``window`` warm-up rows so
    that the first requested date already has a full window of returns ending on
    it.

    The ``sql`` backend gets the returns from ``LAG(close)`` in the database and
    computes the standard deviation in Python; there is no pure-SQL route, because
    a window function cannot be an argument to another window function -- see the
    note on :data:`VOLATILITY_BACKENDS`. The other two backends differ only in
    where the returns came from, which is what makes the three comparable.
    """
    _reject_backend(backend, VOLATILITY_BACKENDS)
    if window < 2:
        raise ValueError("a sample standard deviation needs at least 2 returns")

    series = load_price_series(
        instrument,
        start_date=start_date,
        end_date=end_date,
        pad=window,
        with_previous_close=backend == "sql",
    )

    if backend == "sql":
        returns = [
            point.value
            for point in daily_returns(
                series.dates, series.closes, previous_closes=series.column("previous_close")
            )
        ]
    elif backend == "pandas":
        returns = [
            point.value for point in daily_returns_pandas(series.dates, series.closes)
        ]
    else:
        returns = [point.value for point in daily_returns(series.dates, series.closes)]

    if backend == "pandas":
        points = rolling_volatility_pandas(
            series.dates, returns, window, periods_per_year=periods_per_year
        )
    else:
        points = rolling_volatility(
            series.dates, returns, window, periods_per_year=periods_per_year
        )

    return RollingVolatility(
        symbol=series.symbol,
        backend=backend,
        start_date=start_date,
        end_date=end_date,
        padding=series.padding,
        window=window,
        periods_per_year=periods_per_year,
        points=points[series.padding :],
    )


def maximum_drawdown_for_instrument(
    instrument,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    backend: str = "sql",
) -> MaximumDrawdown:
    """The worst peak-to-trough decline in the range, for one instrument.

    No warm-up row is read, for the same reason as the cumulative returns above:
    the peak is anchored at the **first observation inside the range**, so the
    first point's drawdown is ``0`` and the answer is reproducible from the
    range's own prices. Reading the rows before ``start_date`` would move the peak
    outside the range and report a decline the caller did not ask about -- and it
    cannot be a fixed ``pad`` anyway, since the all-time peak is however far back
    the instrument's first row is. A caller who wants a decline from an earlier
    peak asks for a range that starts earlier, which is the same answer for one
    query instead of an unbounded one.

    The ``sql`` backend gets the running maximum -- the whole of the scan -- from
    ``MAX(close) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)`` and
    computes only the ratio on top of it. That this metric has a SQL member while
    volatility does not is the point of the rule in
    :data:`DRAWDOWN_BACKENDS`: the window here is over the closes, not over another
    window's output.
    """
    _reject_backend(backend, DRAWDOWN_BACKENDS)

    series = load_price_series(
        instrument,
        start_date=start_date,
        end_date=end_date,
        with_running_max=backend == "sql",
    )

    if backend == "sql":
        points = drawdown_series(
            series.dates, series.closes, peaks=series.column("running_max")
        )
    elif backend == "pandas":
        points = drawdown_series_pandas(series.dates, series.closes)
    else:
        points = drawdown_series(series.dates, series.closes)

    return MaximumDrawdown(
        symbol=series.symbol,
        backend=backend,
        start_date=start_date,
        end_date=end_date,
        padding=series.padding,
        summary=summarize_drawdown(points),
        points=points[series.padding :],
    )
