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
    ExtremePoint,
    ExtremesSummary,
    MovingAveragePoint,
    PeriodChange,
    ReturnPoint,
    SelectedPeriod,
    VolatilityPoint,
    VolumePoint,
    VolumeSummary,
    cumulative_returns,
    cumulative_returns_pandas,
    daily_returns,
    daily_returns_pandas,
    drawdown_series,
    drawdown_series_pandas,
    extreme_series,
    extreme_series_pandas,
    moving_average,
    moving_average_pandas,
    moving_average_series,
    period_change,
    period_change_pandas,
    rolling_volatility,
    rolling_volatility_pandas,
    summarize_drawdown,
    summarize_extremes,
    summarize_volume,
    volume_series,
    volume_series_pandas,
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
#: Percentage change over a period has no ``sql`` member for a third and different
#: reason: it has no window at all. Its "window" is a *choice of two rows* -- the
#: first observation at or after the base, the last at or before the end -- and the
#: engine's read boundary returns a series, not a pair. The SQL form would be two
#: ``ORDER BY ... LIMIT 1`` subqueries (which two scalar subqueries do in one
#: statement), a different query shape rather than a window over the rows the loader
#: already has, so it is left out rather than smuggled in as a second read path for
#: a subtraction. Reading the period's rows instead costs one indexed range scan and
#: buys ``observations`` -- how many sessions the period held -- which the
#: two-endpoint query could not report.
#:
#: Maximum drawdown *does* have a SQL member, which is the test of that rule
#: rather than an exception to it: its window -- the running maximum -- is a
#: plain frame over the closes, so ``MAX(close) OVER (ROWS BETWEEN UNBOUNDED
#: PRECEDING AND CURRENT ROW)`` is one legal window, and only the ratio on top of
#: it is arithmetic.
#:
#: Price extremes are the same case with two directions: a running maximum and a
#: running minimum are each a single frame over the closes, so both can be had in
#: the one query.
#:
#: Volume summaries are the same case again, one shape further out: a running sum
#: is a single frame over the ``volume`` column, so the database can carry the
#: total and Python only derives the ratio and the period figures.
DAILY_RETURN_BACKENDS = ("sql", "python", "pandas")
CUMULATIVE_RETURN_BACKENDS = ("python", "pandas")
MOVING_AVERAGE_BACKENDS = ("sql", "python", "pandas")
VOLATILITY_BACKENDS = ("sql", "python", "pandas")
DRAWDOWN_BACKENDS = ("sql", "python", "pandas")
EXTREME_BACKENDS = ("sql", "python", "pandas")
VOLUME_BACKENDS = ("sql", "python", "pandas")
PERIOD_CHANGE_BACKENDS = ("python", "pandas")


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
class NamedMovingAverage:
    """One named moving-average series: the name, its window, and its points."""

    name: str
    window: int
    points: list[MovingAveragePoint]


@dataclass(frozen=True)
class MovingAverages(MetricResult):
    """The conventional moving averages (7, 30, 90), from one read.

    ``series`` holds one entry per named series, in the conventional order, rather
    than one flat list of points: a point belongs to exactly one window, and
    flattening the three would lose the only thing that distinguishes them. There is
    therefore no single ``window`` on this result -- the windows are on the series,
    and :attr:`windows` collects them for a caller checking what came back.
    """

    series: tuple[NamedMovingAverage, ...] = ()

    @property
    def windows(self) -> tuple[int, ...]:
        """The windows computed, in the conventional order."""
        return tuple(named.window for named in self.series)

    def named(self, name: str) -> NamedMovingAverage:
        """One series by name, or ``KeyError`` naming what this result has."""
        for named in self.series:
            if named.name == name:
                return named
        raise KeyError(
            f"{name!r} was not computed; this result has "
            f"{[named.name for named in self.series]}"
        )

    @property
    def undefined(self) -> int:
        """How many points across all of the series carry no value.

        The base class counts ``point.value`` over one ``points`` list and this result
        has three, so it sums over them. One number covering all three is what a
        caller wants to see: a young instrument has a long run of ``None`` at the
        front of the 90-day series and none at the front of the 7-day one, and the sum
        is the total that has to be explained.
        """
        return sum(
            1 for named in self.series for point in named.points if point.value is None
        )


@dataclass(frozen=True)
class PeriodChanges(MetricResult):
    """One answer per selected period, all from the same single read.

    ``points`` is deliberately empty: this metric has no per-date series -- *that*
    series is metric 2's, seen from a different base -- and a point here would be a
    period, not a date, which is what :attr:`periods` holds. A result is built the
    same way for one period as for several, so the shape does not change with the
    number asked for. ``start_date``/``end_date`` echo the span that was read, which
    for several periods is their union.
    """

    periods: tuple[PeriodChange, ...] = ()

    @property
    def undefined(self) -> int:
        """Periods with no measurable change: an empty period, or a base of zero.

        Overrides the base class, which counts ``point.value``; this result's answers
        are periods rather than points.
        """
        return sum(1 for period in self.periods if period.change is None)


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


@dataclass(frozen=True)
class PriceExtremes(MetricResult):
    """One price-extremes answer: the running high and low, and the range's.

    ``summary`` holds both the close extremes and the traded ones; ``points`` hold
    the running close extremes, whose last row is the summary's close pair.
    """

    summary: ExtremesSummary = field(default_factory=ExtremesSummary)
    points: list[ExtremePoint] = field(default_factory=list)

    @property
    def undefined(self) -> int:
        """Always ``0``: an extreme point cannot be missing a value.

        Every row has a close, so the running high and the running low always
        exist; the *traded* extremes are ``None`` only when the whole range is
        empty, and then there are no points to count. This overrides
        :attr:`MetricResult.undefined`, which counts ``point.value`` -- a field this
        point type deliberately does not have. A point here is two prices, and
        calling one of them "value" would invite a reader to treat the other as an
        afterthought.
        """
        return 0


@dataclass(frozen=True)
class VolumeSummaries(MetricResult):
    """One volume answer: the running total per session, and the period's figures.

    ``summary`` holds the period totals and spread; ``points`` hold the running
    total and the relative volume, whose last cumulative is the summary's
    ``total_volume``.
    """

    summary: VolumeSummary = field(default_factory=VolumeSummary)
    points: list[VolumePoint] = field(default_factory=list)

    @property
    def undefined(self) -> int:
        """Points with no relative volume: a prefix in which nothing had traded.

        ``relative_volume`` is ``None`` while the running total is ``0``, because
        ``volume / 0`` is not a ratio. It is usually the first point only -- and only
        when the range opens on a zero-volume session -- so this counts a real,
        explainable hole rather than a rounding artefact.
        """
        return sum(1 for point in self.points if point.relative_volume is None)


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


def moving_averages_for_instrument(
    instrument,
    *,
    names: Sequence[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    backend: str = "sql",
) -> MovingAverages:
    """The conventional moving averages for one instrument, in one query.

    ``names`` selects from the closed vocabulary -- ``moving_average_7``,
    ``moving_average_30``, ``moving_average_90`` -- and defaults to all three. There
    is deliberately no ``window`` parameter: the windows a report quotes are a
    convention rather than a knob, and naming them is what makes one 7-day average
    comparable with another 7-day average instead of with whatever number happened to
    be passed.

    The loader reads ``max(window) - 1`` observations before ``start_date``, so the
    first requested date already has a full window for the longest series asked for.
    This is the metric that makes the loader's general ``pad`` worth having: one row
    of warm-up was enough for a return, a 90-day average needs 89.

    Asking for all three costs **one** read of ``pad + observations`` rows, and
    answering all three from it costs three rolling sums over rows already in memory.
    Three separate calls would read 6, 29 and 89 warm-up rows over three queries;
    this reads 89 over one. What is given up is stated rather than hidden: a caller
    who wants only the 7-day series still reads 89 warm-up rows, which is why
    ``names`` exists.

    In the ``sql`` backend the three means come from three ``AVG(close) OVER (ROWS
    BETWEEN window - 1 PRECEDING AND CURRENT ROW)`` annotations over the same rows,
    evaluated in one pass over one sorted buffer; the other two compute them in
    Python over the same rows. All three agree, including on the partial windows at
    the start of a history shorter than the lookback, because the completeness rule
    (``sample_size == window``) is applied identically to a supplied mean.
    """
    _reject_backend(backend, MOVING_AVERAGE_BACKENDS)

    wanted = moving_average_series(names)
    if not wanted:
        # An empty selection asks for nothing, so it reads nothing.
        return MovingAverages(
            symbol=instrument.symbol,
            backend=backend,
            start_date=start_date,
            end_date=end_date,
            padding=0,
            series=(),
        )

    windows = tuple(average.window for average in wanted)
    data = load_price_series(
        instrument,
        start_date=start_date,
        end_date=end_date,
        pad=max(windows) - 1,
        moving_average_windows=windows if backend == "sql" else (),
    )

    computed: list[NamedMovingAverage] = []
    for average in wanted:
        if backend == "sql":
            points = moving_average(
                data.dates,
                data.closes,
                average.window,
                values=data.column(f"moving_average_{average.window}"),
            )
        elif backend == "pandas":
            points = moving_average_pandas(data.dates, data.closes, average.window)
        else:
            points = moving_average(data.dates, data.closes, average.window)

        computed.append(
            NamedMovingAverage(
                name=average.name,
                window=average.window,
                points=points[data.padding :],
            )
        )

    return MovingAverages(
        symbol=data.symbol,
        backend=backend,
        start_date=start_date,
        end_date=end_date,
        padding=data.padding,
        series=tuple(computed),
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


def price_extremes_for_instrument(
    instrument,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    backend: str = "sql",
) -> PriceExtremes:
    """The highest and lowest prices of a range, for one instrument.

    Both readings of "the highest price" are reported, because a report asks both
    and the engine should not choose silently:

    * the extremes of the **closes** (``highest_close``/``lowest_close``), the
      reference price every other metric is built on -- and, per date, the running
      extremes, which is the series whose last row is that pair;
    * the extremes of the **traded range** (``highest_high``/``lowest_low``), from
      the ``high``/``low`` columns, which is what the market actually touched.

    The anchors are inside the range, as with the drawdown: ``pad=0``, no warm-up
    row, and the first point both the highest and the lowest close so far. This is
    also the first metric to read columns other than ``close``, which is what the
    loader's ``fields`` parameter has been for since metric 1.

    The ``sql`` backend gets the running maximum and minimum from two single
    windows over the closes -- legal in one query, unlike a window over another
    window's output -- and everything on top of them is the shared code path.
    """
    _reject_backend(backend, EXTREME_BACKENDS)

    series = load_price_series(
        instrument,
        start_date=start_date,
        end_date=end_date,
        fields=("high", "low"),
        with_running_extremes=backend == "sql",
    )

    if backend == "sql":
        points = extreme_series(
            series.dates,
            series.closes,
            highest=series.column("running_max"),
            lowest=series.column("running_min"),
        )
    elif backend == "pandas":
        points = extreme_series_pandas(series.dates, series.closes)
    else:
        points = extreme_series(series.dates, series.closes)

    summary = summarize_extremes(
        series.dates,
        points,
        highs=series.column("high"),
        lows=series.column("low"),
    )

    return PriceExtremes(
        symbol=series.symbol,
        backend=backend,
        start_date=start_date,
        end_date=end_date,
        padding=series.padding,
        summary=summary,
        points=points[series.padding :],
    )


def volume_summaries_for_instrument(
    instrument,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    backend: str = "sql",
) -> VolumeSummaries:
    """Volume for one instrument's range: the running total, and the period's.

    The period figures answer the questions a volume report is made of — how much
    traded, what a typical session looked like, which sessions were heaviest and
    lightest, and how the sessions were spread around the average — and the points
    make them checkable session by session.

    The anchors are inside the range, as they are for the cumulative return, the
    drawdown and the extremes: the running total starts at the first requested
    session and ``padding`` is ``0``. A caller who wants a total that includes
    earlier sessions asks for a range that starts earlier; a running total that
    reached back arbitrarily far would be an unbounded read, which is the same
    reason the all-time peak is not offered.

    The ``sql`` backend gets the running total from
    ``SUM(volume) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)`` — a
    single frame over a column the row already carries — and derives the mean and
    the ratio here. This is the third metric whose SQL form is one legal window,
    and the reason it has one is the reason volatility does not: the window is over
    the *volume*, not over another window's output.
    """
    _reject_backend(backend, VOLUME_BACKENDS)

    series = load_price_series(
        instrument,
        start_date=start_date,
        end_date=end_date,
        fields=("volume",),
        with_running_total=backend == "sql",
    )
    volumes = series.column("volume")

    if backend == "sql":
        points = volume_series(
            series.dates, volumes, cumulative=series.column("running_total")
        )
    elif backend == "pandas":
        points = volume_series_pandas(series.dates, volumes)
    else:
        points = volume_series(series.dates, volumes)

    return VolumeSummaries(
        symbol=series.symbol,
        backend=backend,
        start_date=start_date,
        end_date=end_date,
        padding=series.padding,
        summary=summarize_volume(points),
        points=points[series.padding :],
    )


def percentage_change_for_instrument(
    instrument,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    periods: Sequence[SelectedPeriod] | None = None,
    backend: str = "python",
) -> PeriodChanges:
    """The price change over each selected period, for one instrument.

    One period, from ``start_date`` to ``end_date``, or several from ``periods`` --
    a sequence of :class:`~apps.analytics.metrics.SelectedPeriod`, which is a label
    and two bounds. The two forms are mutually exclusive, because accepting both
    invites a caller to pass a range *and* a list of ranges and have one of them
    silently ignored.

    **Every period is answered from one read.** The loader reads the union of the
    requested spans once and each period's bounds are snapped inside it, so "the
    change this week, this month and this quarter" is one query rather than three --
    which is the whole reason ``periods`` exists.

    The base and the end of a period are the first and last *observations* inside its
    bounds, and both are reported back on the answer: a request names calendar dates,
    and a report that says "1 March to 31 March" is quoting the sessions in between.
    ``pad`` is ``0``, because a base that snapped to the row before the period would
    be the change *into* the period -- a different question, and one that metric 1
    answers for that date.

    For a period that is exactly the requested range, the answer is the number metric
    2's series ends on: ``close(last) / close(first) - 1``. It is computed here from
    the two prices rather than by taking that series' last point, and a test asserts
    the two agree -- the series and the endpoint are the same fact seen twice, which
    is the only thing that makes the second one worth having.

    There is no ``sql`` backend -- see :data:`PERIOD_CHANGE_BACKENDS` -- so ``python``
    is the default, and ``pandas`` locates the bounds by binary search instead of a
    scan.
    """
    _reject_backend(backend, PERIOD_CHANGE_BACKENDS)

    if periods is None:
        selected: tuple[SelectedPeriod, ...] = (
            SelectedPeriod(label=None, start_date=start_date, end_date=end_date),
        )
    else:
        if start_date is not None or end_date is not None:
            raise ValueError("pass either start_date/end_date or periods, not both")
        selected = tuple(periods)

    if not selected:
        # An empty selection asks for nothing, so it reads nothing.
        return PeriodChanges(
            symbol=instrument.symbol, backend=backend, padding=0, periods=()
        )

    # The union of the requested spans. A period with no bound means "from the
    # beginning" or "to the end" of the series, so the union loses that bound too and
    # the read starts at the first row rather than at a date.
    read_start = (
        None
        if any(period.start_date is None for period in selected)
        else min(period.start_date for period in selected)
    )
    read_end = (
        None
        if any(period.end_date is None for period in selected)
        else max(period.end_date for period in selected)
    )

    data = load_price_series(instrument, start_date=read_start, end_date=read_end)

    compute = period_change_pandas if backend == "pandas" else period_change
    changes = tuple(
        compute(
            data.dates,
            data.closes,
            start_date=period.start_date,
            end_date=period.end_date,
            label=period.label,
        )
        for period in selected
    )

    return PeriodChanges(
        symbol=data.symbol,
        backend=backend,
        start_date=read_start,
        end_date=read_end,
        padding=data.padding,
        periods=changes,
    )
