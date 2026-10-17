"""Business logic for analytics: what the API calls, and what it calls into.

Two halves. The first is snapshot reads, which are stored numbers. The second is
the metric entry points, which turn a request -- one instrument, a date range, a
backend -- into a calculation. The entry points are thin on purpose: they choose
a loader and a calculation and hand back the pair, so the decisions a reader
needs to audit (which rows were read, which arithmetic was applied) are visible
in one place instead of spread through a view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .metrics import ReturnPoint, daily_returns, daily_returns_pandas
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
# Daily simple returns
# --------------------------------------------------------------------------- #

#: The interchangeable calculation backends.
#:
#: * ``sql``    -- the previous close comes from ``LAG(close)`` in the database;
#: * ``python`` -- the previous close is the previous item of a list;
#: * ``pandas`` -- the previous close is ``Series.shift(1)``.
#:
#: All three produce the same points; the tests assert it row for row. ``sql`` is
#: the default because the engine's canonical path keeps the window in the
#: database -- but note the honest limit of that claim: for a single instrument
#: the rows are in memory either way, so the shift is the same work and the SQL
#: path even returns one extra column. ``LAG`` earns its place as the series
#: grows, when the windowed value is consumed in the database (a rank, a filter
#: on the change) or when the alternative would be shipping the rows to compute a
#: shift -- which is the cross-sectional case, not this one.
DAILY_RETURN_BACKENDS = ("sql", "python", "pandas")


@dataclass(frozen=True)
class DailyReturns:
    """One daily-returns answer, with the inputs that produced it.

    ``points`` covers the requested range only: the warm-up row the loader read
    is accounted for by ``padding`` rather than returned as if it were an answer.
    """

    symbol: str
    backend: str
    start_date: date | None
    end_date: date | None
    padding: int
    points: list[ReturnPoint] = field(default_factory=list)

    @property
    def undefined(self) -> int:
        """How many points have no value, and therefore no return.

        Reported rather than dropped: a hole in a series is a fact about the data
        (the first day, a zero denominator), and a count is how a caller notices
        it without inspecting every row.
        """
        return sum(1 for point in self.points if point.value is None)


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
    if backend not in DAILY_RETURN_BACKENDS:
        raise ValueError(
            f"unknown backend {backend!r}; expected one of "
            f"{', '.join(DAILY_RETURN_BACKENDS)}"
        )

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
