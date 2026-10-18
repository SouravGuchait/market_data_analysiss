"""The read boundary: one instrument in, one ordered series out, one query.

Everything that touches the database for a metric goes through here, so the
retrieval policy is stated once:

* **One query per series.** Not one per metric, not one per metric plus one for
  metadata. The instrument and its exchange are not joined, because a metric
  needs the symbol and nothing else from that table, and the symbol is already
  on the ``Instrument`` instance the caller resolved.
* **Only the columns a metric reads.** ``source``, ``ingestion_job_id`` and
  ``created_at`` are never selected: they cost bytes, parse time and memory, and
  no metric looks at them.
* **Warm-up rows are part of the query, not a second one.** A daily return needs
  the close *before* the first requested date, and a 90-day moving average needs
  89 rows before it. Asking for the range alone would make the first answer of
  every request wrong, which is the classic window-function trap documented in
  ``apps/marketdata/tests/test_exercises.py``: ``WHERE`` is evaluated before the
  window, so a filtered series has no neighbour to lag to.

The one exception to the second bullet's shape is the *cross-section*
(:func:`load_price_cross_section`), which reads many instruments in the one
query and therefore joins the instrument table for the symbols -- there is no
``Instrument`` instance in the caller's hand to take them from. It states its own
departures; everything else here is one instrument at a time.

This module also owns the definition of a *trading day*: it is an observation --
a row -- and never a calendar day. See :func:`missing_trading_dates` for the
other half of that distinction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import (
    Avg,
    DateField,
    DecimalField,
    F,
    Max,
    Min,
    Q,
    RowRange,
    Subquery,
    Sum,
    Window,
)
from django.db.models.functions import Coalesce, Lag

from apps.marketdata.models import PriceRecord, TradingCalendar

#: Columns a metric may ask for beyond the date/close spine.
ALLOWED_FIELDS = ("open", "high", "low", "adjusted_close", "volume")

#: ``close`` is always read: it is the reference price every metric is anchored
#: on, and a second query to fetch a volume-only series would cost more than the
#: one extra column does.
ALWAYS_READ = ("close",)

#: The output field of the window annotations. Declared explicitly because MySQL
#: and SQLite disagree about the type of an undeclared window expression, and
#: Django would have to guess.
PRICE_FIELD = DecimalField(max_digits=18, decimal_places=8)

#: The output field of ``LAG(close)``: a price, like the column it reads.
PREVIOUS_CLOSE_FIELD = PRICE_FIELD


class PriceSeriesError(ValueError):
    """Raised for a series that cannot be trusted to be one.

    A contradiction in the data is not something a metric may quietly pick a
    side of: see :meth:`PriceSeries.assert_one_row_per_date`.
    """


@dataclass(frozen=True)
class PriceSeries:
    """Dates and columns for one instrument, oldest first.

    Deliberately a plain container of tuples rather than a DataFrame, a
    queryset, or a list of model instances: it is what the loader produces and
    what a metric consumes, so neither has to know about the other's shape.
    """

    symbol: str
    dates: tuple[date, ...]
    columns: Mapping[str, tuple]

    #: How many leading rows are warm-up, and therefore outside the range the
    #: caller asked for. Counted from the rows actually returned, not from the
    #: number requested -- a series that begins after ``start_date`` has less.
    padding: int = 0

    def __len__(self) -> int:
        return len(self.dates)

    @property
    def closes(self) -> tuple[Decimal, ...]:
        return self.column("close")

    def column(self, name: str) -> tuple:
        """The values of one column, aligned with :attr:`dates`.

        Raises ``KeyError`` rather than returning an empty tuple: asking for a
        column the query did not select is a programming error, and an empty
        result would turn it into a quietly wrong metric.
        """
        try:
            return self.columns[name]
        except KeyError as exc:
            raise KeyError(
                f"column {name!r} was not loaded; this series has "
                f"{sorted(self.columns)}"
            ) from exc

    def in_range(self) -> PriceSeries:
        """The same series without its warm-up rows.

        This is the boundary between "what was read" and "what was asked for":
        the warm-up rows exist only to make the first requested answer correct,
        and returning them would answer a question nobody asked.
        """
        if not self.padding:
            return self
        cut = slice(self.padding, None)
        return PriceSeries(
            symbol=self.symbol,
            dates=self.dates[cut],
            columns={name: values[cut] for name, values in self.columns.items()},
            padding=0,
        )

    def tail(self, count: int) -> PriceSeries:
        """The newest ``count`` observations, or the whole series if it is shorter.

        The cross-section's window. A ranking measures every instrument over the
        same *number of sessions*, so the window is a slice off the end of each
        series rather than a date range applied to all of them -- and an instrument
        with fewer than ``count`` observations gets all it has, which the ranking
        then reports as a short window instead of presenting a partial window as a
        full one.
        """
        if count < 1:
            raise ValueError("count must be at least 1")
        if count >= len(self.dates):
            return self
        start = len(self.dates) - count
        return PriceSeries(
            symbol=self.symbol,
            dates=self.dates[start:],
            columns={name: values[start:] for name, values in self.columns.items()},
            # Warm-up rows live at the front, so a tail either keeps them all or
            # drops the ones that fell off: the count is what survives the cut.
            padding=max(0, self.padding - start),
        )

    def assert_one_row_per_date(self) -> None:
        """Fails if two rows claim the same trading day, or dates are unordered.

        The table cannot produce this -- ``uniq_price_instrument_trading_date`` is
        unique, and the loader orders by date -- so this is a guard on *other*
        sources: a hand-built series in a test, a frame read from a file, a future
        import that bypasses the pipeline.

        It raises rather than deduplicating because deduplication would have to
        decide which of two contradictory closes for one day is the real one, and
        either choice fabricates a price. Fixing the source is the only correct
        response. (Re-ingestion through the pipeline cannot create this: it
        upserts on ``(instrument, trading_date)``, so the corrected price replaces
        the old row rather than joining it.)
        """
        for earlier, later in zip(self.dates, self.dates[1:]):
            if later <= earlier:
                raise PriceSeriesError(
                    f"{self.symbol}: dates must be strictly increasing, "
                    f"found {earlier} then {later}"
                )

    def to_frame(self):
        """A pandas DataFrame indexed by date, for the vectorised backend.

        Imported locally for the same reason the pandas metrics are: the request
        path should not pay for the import unless it asks for it.
        """
        import pandas as pd

        frame = pd.DataFrame(dict(self.columns), index=pd.Index(self.dates, name="trading_date"))
        frame.index = pd.to_datetime(frame.index)
        return frame


@dataclass(frozen=True)
class CrossSection:
    """Many instruments' prices, grouped, oldest first within each.

    The shape a *cross-sectional* metric reads: one query for a whole universe
    rather than one query per instrument. Keyed by instrument id rather than by
    symbol, because a symbol is display text -- it can be renamed, and a caller's
    filter list can name two different instruments with one -- while a ranking must
    place the instruments it actually read.

    ``series`` is the whole of the state: a caller iterates the instruments in the
    order the query returned them (ascending id), and asks each one for its
    observations. There is no ``padding``, because a cross-section is read without
    warm-up -- its window is a *count of newest observations per instrument*, which
    is decided after the read.
    """

    series: Mapping[int, PriceSeries]

    def __len__(self) -> int:
        return len(self.series)

    @property
    def instruments(self) -> tuple[int, ...]:
        """The instrument ids, in ascending order."""
        return tuple(self.series)

    def to_frame(self):
        """The whole cross-section as one DataFrame, one row per observation.

        Imported locally, like every pandas touchpoint: a request that does not ask
        for this backend should not pay for the import.

        The instrument id is an ordinary column rather than the index, because a
        ranking groups *by* it and an index level would only have to be reset again.
        Dates stay ``datetime.date`` objects rather than becoming timestamps, so a
        value taken out of a group here is the same value the other backends carry.
        """
        import pandas as pd

        if not self.series:
            return pd.DataFrame(columns=["instrument_id", "symbol", "trading_date"])

        # Every series carries the same columns in the same order -- they come from
        # one query -- so any one of them names the frame's columns too.
        first = next(iter(self.series.values()))
        names = tuple(first.columns)

        records = [
            (
                instrument_id,
                series.symbol,
                trading_date,
                *(series.columns[name][index] for name in names),
            )
            for instrument_id, series in self.series.items()
            for index, trading_date in enumerate(series.dates)
        ]

        return pd.DataFrame.from_records(
            records,
            columns=["instrument_id", "symbol", "trading_date", *names],
        )


def load_price_cross_section(
    *,
    instrument_ids: Sequence[int] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    fields: Sequence[str] = (),
) -> CrossSection:
    """Reads many instruments' prices in one query, grouped by instrument.

    This is the ranking's read: one query for the whole universe, ordered so that
    each instrument's observations come out oldest first. It is the same retrieval
    discipline as :func:`load_price_series`, with two differences that follow from
    reading many instruments at once:

    * **The instrument table is joined.** The per-series loader deliberately does
      not join it: the caller already holds the ``Instrument`` and a metric needs
      only its symbol. A cross-section reads instruments the caller never resolved,
      so the symbol has to come from the query. The join is on a primary key, so it
      is a single-row index lookup per price row, and the alternative -- a second
      query for the symbols -- would give up the one-query rule to save less than
      the join costs.
    * **The instrument id is part of the row.** It is the key of the returned
      mapping, not the symbol: see :class:`CrossSection`.

    There is no warm-up and no window here, unlike every other read in this module,
    and that is the metric's shape rather than an omission. The window a ranking
    needs is "the newest *n* observations **of each** instrument"; *n* is per
    instrument while the read is per range, so the slice happens after the read, in
    the metric. That makes ``start_date``/``end_date`` the hard bound on the work --
    see :func:`apps.analytics.services.ranking_for_instruments` for what that means
    for a request that supplies only a count.
    """
    unknown = sorted(set(fields) - set(ALLOWED_FIELDS))
    if unknown:
        raise ValueError(
            f"unknown price fields: {', '.join(unknown)}; "
            f"allowed: {', '.join(ALLOWED_FIELDS)}"
        )

    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date")

    read = [*ALWAYS_READ, *(name for name in fields if name not in ALWAYS_READ)]
    names = ["instrument_id", "instrument__symbol", "trading_date", *read]

    queryset = PriceRecord.objects.all()
    if instrument_ids is not None:
        queryset = queryset.filter(instrument_id__in=list(instrument_ids))
    if start_date:
        queryset = queryset.filter(trading_date__gte=start_date)
    if end_date:
        queryset = queryset.filter(trading_date__lte=end_date)

    rows = list(queryset.order_by("instrument_id", "trading_date").values_list(*names))

    by_instrument: dict[int, list[tuple]] = {}
    for row in rows:
        by_instrument.setdefault(row[0], []).append(row)

    series: dict[int, PriceSeries] = {}
    for instrument_id, group in by_instrument.items():
        one = PriceSeries(
            symbol=group[0][1],
            dates=tuple(row[2] for row in group),
            columns={
                name: tuple(row[3 + position] for row in group)
                for position, name in enumerate(read)
            },
        )
        # The same guard as the single-instrument loader, at the same cost: the
        # ordering this query imposes would hide a contradiction rather than fix it.
        one.assert_one_row_per_date()
        series[instrument_id] = one

    return CrossSection(series=series)


def _warm_up_boundary(instrument, start_date: date, pad: int):
    """The earliest date the query must look back to, as inline subqueries.

    ``pad=1`` is the previous session; ``pad=89`` is the first row a 90-day
    moving average needs in order to have a full window on ``start_date``.
    Selecting a *date* rather than a row is what lets the caller's single query
    return the warm-up rows and the requested rows together, instead of reading
    twice.

    The ``COALESCE`` is the interesting part. When the series is shorter than
    ``pad`` -- a young instrument, or a lookback longer than its life -- the
    offset subquery finds no row and returns NULL, and ``trading_date >= NULL`` is
    *unknown*, which ``WHERE`` reads as false. Every requested row would vanish,
    which is the opposite of the intent. Falling back to the instrument's
    earliest date turns that case into "look back as far as there is anything to
    look at", which is exactly the requested behaviour when the history is
    shorter than the bound. The cost stays bounded either way: the fallback is
    only reachable when there is less than ``pad`` rows of history, so there is
    nothing further back to scan.
    """
    boundary = Subquery(
        PriceRecord.objects.filter(instrument=instrument, trading_date__lt=start_date)
        .order_by("-trading_date")
        .values("trading_date")[pad - 1 : pad],
        output_field=DateField(),
    )
    earliest = Subquery(
        PriceRecord.objects.filter(instrument=instrument)
        .order_by("trading_date")
        .values("trading_date")[:1],
        output_field=DateField(),
    )
    return Coalesce(boundary, earliest, output_field=DateField())


def load_price_series(
    instrument,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    pad: int = 0,
    fields: Sequence[str] = (),
    with_previous_close: bool = False,
    moving_average_windows: Sequence[int] = (),
    with_running_max: bool = False,
    with_running_extremes: bool = False,
    with_running_total: bool = False,
) -> PriceSeries:
    """Reads one instrument's prices as an ordered, gap-tolerant series.

    Parameters
    ----------
    start_date, end_date
        Inclusive calendar bounds on which *observations* to return. A trading day
        with no row simply is not in the series; the bounds do not invent one.
    pad
        Observations to include *before* ``start_date`` so that a window has a
        warm-up. ``pad=1`` for a daily return, ``pad=window-1`` for a moving
        average. Bounded by the history that exists, so a short series returns
        what it has instead of failing.
    fields
        Extra columns to read, from :data:`ALLOWED_FIELDS`. ``close`` is always
        read.
    with_previous_close
        Add ``LAG(close)`` as a ``previous_close`` column. The window is
        partitioned by instrument and ordered by date, so it is the previous
        *session*, not the previous row of some other instrument. Note that the
        window is computed over the rows this query returns -- which is exactly
        why ``pad`` exists.
    moving_average_windows
        Add ``AVG(close) OVER (ROWS BETWEEN window - 1 PRECEDING AND CURRENT ROW)``
        as a ``moving_average_<window>`` column, for each window asked for: the
        trailing mean the metric would otherwise compute in Python, computed by the
        database instead. A sequence rather than one window because the conventional
        averages are computed together -- three windows over one read cost one query,
        one sort and one buffer, where three calls would cost three of each, and the
        single ``pad`` covers them all.
    with_running_max
        Add ``MAX(close) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)``
        as a ``running_max`` column: the highest close so far, which is the peak a
        drawdown is measured from. Its frame is unbounded to the start of the rows
        returned, so with ``pad=0`` the peak is the range's own -- which is the
        anchoring :func:`apps.analytics.metrics.drawdown_series` documents.
    with_running_extremes
        Add both ``running_max`` and ``running_min`` over the same frame: the
        highest and lowest close so far, for the price-extremes metric. Two single
        windows, so still one query.
    with_running_total
        Add ``SUM(volume) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT
        ROW)`` as a ``running_total`` column: the volume traded in the range so
        far. Needs only the ``volume`` column, which the caller asks for through
        ``fields``; the window sums a column the row already carries rather than a
        second read of it.

    The window columns are parameters of *this* function rather than SQL handed in
    by a caller, because a window and the padding that makes it correct are one
    decision. A caller who supplied the expression separately could ask for a
    90-row average with ``pad=1`` and get a number that is quietly wrong at the
    start of the range; here the boundary and the frame come from the same call.
    They are the shapes the engine needs -- the previous session, the trailing
    mean, and the running maximum -- and none is general-purpose.

    Returns
    -------
    PriceSeries
        Ordered oldest first, with :attr:`PriceSeries.padding` telling the caller
        how many leading rows were warm-up.

    The one query is a range scan on the instrument's index. Its cost grows with
    the rows returned, not with the size of the table, which is what makes the
    date range a hard bound on the work rather than a filter applied afterwards.
    """
    if pad < 0:
        raise ValueError("pad must not be negative")

    unknown = sorted(set(fields) - set(ALLOWED_FIELDS))
    if unknown:
        raise ValueError(
            f"unknown price fields: {', '.join(unknown)}; "
            f"allowed: {', '.join(ALLOWED_FIELDS)}"
        )

    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date")

    read = [*ALWAYS_READ, *(name for name in fields if name not in ALWAYS_READ)]

    window_columns: dict[str, Window] = {}
    if with_previous_close:
        window_columns["previous_close"] = Window(
            expression=Lag("close", output_field=PRICE_FIELD),
            partition_by=[F("instrument_id")],
            order_by=F("trading_date").asc(),
        )
    if moving_average_windows:
        for window in moving_average_windows:
            if window < 1:
                raise ValueError("moving_average_windows must all be at least 1")
            window_columns[f"moving_average_{window}"] = Window(
                expression=Avg("close", output_field=PRICE_FIELD),
                partition_by=[F("instrument_id")],
                order_by=F("trading_date").asc(),
                # The trailing mean, the current row included. A database frame
                # averages however many rows it has, so the first rows of a range
                # are a partial mean; the metric applies the completeness rule, and
                # the two routes agree because that rule is stated in one place.
                #
                # One column per window, named after it: three frame aggregates
                # over the same rows, which the database computes in one pass over
                # one sorted buffer rather than three.
                frame=RowRange(start=-(window - 1), end=0),
            )
    if with_running_max:
        window_columns["running_max"] = Window(
            expression=Max("close", output_field=PRICE_FIELD),
            partition_by=[F("instrument_id")],
            order_by=F("trading_date").asc(),
            # Unbounded to the start of the rows this query returns, which with
            # ``pad=0`` is the start of the requested range. It is a real window
            # -- a running maximum is a scan -- so unlike the deviation it can be
            # had from the database in one level.
            frame=RowRange(start=None, end=0),
        )
    if with_running_extremes:
        # The same frame for the other direction. Both are single windows over the
        # closes, so both are legal in one query; a metric needing the *deviation*
        # of a running value could not do this (see the note in ``services``).
        window_columns["running_max"] = Window(
            expression=Max("close", output_field=PRICE_FIELD),
            partition_by=[F("instrument_id")],
            order_by=F("trading_date").asc(),
            frame=RowRange(start=None, end=0),
        )
        window_columns["running_min"] = Window(
            expression=Min("close", output_field=PRICE_FIELD),
            partition_by=[F("instrument_id")],
            order_by=F("trading_date").asc(),
            frame=RowRange(start=None, end=0),
        )
    if with_running_total:
        # The third shape of frame over the same rows: a running sum rather than a
        # running extreme. Note there is no ``output_field``: the sum of a BIGINT is
        # a BIGINT, and telling Django it is a DECIMAL(18,8) would invite it to
        # convert a count as if it were money.
        window_columns["running_total"] = Window(
            expression=Sum("volume"),
            partition_by=[F("instrument_id")],
            order_by=F("trading_date").asc(),
            frame=RowRange(start=None, end=0),
        )

    names = ["trading_date", *read, *window_columns]
    queryset = PriceRecord.objects.filter(instrument=instrument)

    if end_date:
        queryset = queryset.filter(trading_date__lte=end_date)

    if start_date:
        # ``pad`` rows back, or as far back as the history goes when it is
        # shorter than that -- see :func:`_warm_up_boundary`.
        lower = _warm_up_boundary(instrument, start_date, pad) if pad else start_date
        queryset = queryset.filter(trading_date__gte=lower)

    if window_columns:
        queryset = queryset.annotate(**window_columns)

    rows = list(queryset.order_by("trading_date").values_list(*names))

    dates = tuple(row[0] for row in rows)
    columns = {
        name: tuple(row[position] for row in rows)
        for position, name in enumerate(read, start=1)
    }
    for offset, name in enumerate(window_columns, start=1):
        columns[name] = tuple(row[len(read) + offset] for row in rows)

    series = PriceSeries(
        symbol=instrument.symbol,
        dates=dates,
        columns=columns,
        padding=sum(1 for value in dates if start_date and value < start_date),
    )
    series.assert_one_row_per_date()
    return series


def calendar_sessions(instrument, start_date: date, end_date: date):
    """The calendar sessions open to one instrument over a date range.

    Returns a queryset of :class:`~apps.marketdata.models.TradingCalendar` rows,
    so a caller may count it or exclude the priced dates from it. The optional
    exchange on a calendar row is honoured: a row scoped to another exchange is
    not this instrument's session, while an unscoped row applies to all of them.
    Defined once because both :func:`missing_trading_dates` and the quality
    summary need the same scope; two copies could disagree about which sessions
    existed.
    """
    calendar = TradingCalendar.objects.filter(date__gte=start_date, date__lte=end_date)
    if instrument.exchange_id is not None:
        calendar = calendar.filter(
            Q(exchange_id=instrument.exchange_id) | Q(exchange__isnull=True)
        )
    return calendar


def missing_trading_dates(
    instrument,
    start_date: date,
    end_date: date,
) -> list[date]:
    """Sessions the exchange was open that this instrument has no price for.

    This is the other half of "trading days": a gap in a series is invisible to
    the metrics -- the window simply closes over it -- but it is not invisible to
    a person auditing the data, and the two questions need different answers.

    The calendar marks a date as a session; a price is evidence that this
    instrument traded. A date with neither is a market holiday and is not
    reported. The optional exchange on the calendar row is honoured: a row
    scoped to another exchange is not this instrument's session, while an
    unscoped row is taken to apply to all of them.

    One query, and it is a join the database does well: the calendar is small and
    the price side is an index range scan.
    """
    covered = PriceRecord.objects.filter(
        instrument=instrument, trading_date__gte=start_date, trading_date__lte=end_date
    ).values("trading_date")

    return list(
        calendar_sessions(instrument, start_date, end_date)
        .exclude(date__in=covered)
        .order_by("date")
        .values_list("date", flat=True)
    )
