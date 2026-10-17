"""Pure metric calculations: numbers in, numbers out, no Django and no I/O.

The app's division of labour is:

* :mod:`apps.analytics.series` reads rows -- one query, only the columns a metric
  needs, with the warm-up rows a window requires;
* **this module** turns a sequence of prices into a number, and imports nothing
  from Django, so a metric can be checked against a hand-computed series without
  a database and reused by an offline path that has a DataFrame, not a queryset;
* :mod:`apps.analytics.services` is what the API calls: it chooses a loader, a
  calculation backend, and a date range, and returns one result.

Three conventions are fixed here, once, because every metric in the engine
inherits them.

**Money is ``Decimal``.** Prices are ``DECIMAL(18,8)`` in MySQL and stay exact
Decimal in Python: ``(102 - 100) / 100`` is ``0.02``, not ``0.020000000000000004``.
Levels built from prices (a moving average) are money and are quantized as money;
ratios are quantized as ratios. Two names, because they are eight places for two
different reasons.

**Ratios are ``Decimal``, quantized to :data:`RATIO_DECIMAL_PLACES` places.**
That is the width of ``AnalyticSnapshot.value`` (``DECIMAL(18,8)``), so a return
computed on demand and a return read back from a stored snapshot are the same
number at the same precision -- one precision in the system, not two. Eight
places is a ten-thousandth of a basis point: far below any market's tick.

**Undefined is ``None``, never ``0`` and never ``inf``.** "No value" is a real
answer here (the first day of a series has no return), and a zero would be a
claim that the price did not move. It is serialized as JSON ``null``.

Inputs arrive normalised through ``Decimal(...)`` first, because what a database
driver hands back for a ``DECIMAL(18,8)`` column is not one type: MySQL returns
``Decimal('100.00000000')``, SQLite returns the same number with different
trailing zeros, and a window-function annotation skips the field's converter on
both. Normalising once, at the top of each metric, is what makes the arithmetic
backend-independent. It is deliberately only the *ratios* that are quantized:
money is passed through at whatever precision the store holds, because rendering
a price is the serializer's decision, not a metric's.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

#: The quantum a ratio is rounded to, and its number of decimal places.
#: Kept in step with ``AnalyticSnapshot.value`` -- see the module docstring.
RATIO_EXPONENT = Decimal("0.00000001")
RATIO_DECIMAL_PLACES = 8

#: The quantum a *price* or a price-derived level (a moving average) is rounded
#: to. The same eight places as a ratio today, and deliberately a separate name:
#: they are eight places for different reasons -- the width of the price column
#: versus the reported precision of a ratio -- and a schema that widened its
#: prices should not silently change how a return is reported.
MONEY_EXPONENT = Decimal("0.00000001")
MONEY_DECIMAL_PLACES = 8

#: Trading sessions in a year, for annualizing a per-session statistic: 52 weeks
#: x 5 weekdays, less public holidays. The convention, and a model -- see
#: :func:`rolling_volatility`.
TRADING_DAYS_PER_YEAR = 252


def quantize_ratio(value: Decimal) -> Decimal:
    """Rounds a ratio to the engine's reporting precision.

    ``ROUND_HALF_UP`` rather than Python's default banker's rounding: this value
    ends up in a report, and "half rounds away from zero" is what a reader of a
    percentage expects. It is symmetric about zero, so a gain and an equal loss
    round to the same magnitude.
    """
    return value.quantize(RATIO_EXPONENT, rounding=ROUND_HALF_UP)


def quantize_price(value: Decimal) -> Decimal:
    """Rounds a price -- or a level built from prices, such as a moving average
    -- to the store's width, ``DECIMAL(18,8)``."""
    return value.quantize(MONEY_EXPONENT, rounding=ROUND_HALF_UP)


def format_decimal(value: Decimal | None) -> str | None:
    """The engine's rendering of a value: fixed point, eight places.

    ``str(Decimal("0.00000000"))`` is ``"0E-8"``, which is not what a report
    should show for a zero return -- and it is what a naive JSON encoder would
    emit. Formatting in fixed point explicitly makes a zero look like every other
    value, so this is what a payload renders and what the tests compare. It is
    also why the tests assert on this rather than on ``str``: a difference in
    rendering should not be able to hide behind a test helper.
    """
    return None if value is None else format(value, "f")


@dataclass(frozen=True)
class ReturnPoint:
    """One observation, the return it produces, and the inputs to that return.

    The inputs are kept rather than discarded because they *are* the
    intermediate calculation: ``(104 - 103) / 103`` is what lets a person check
    the answer, and a response can show it without a second query.
    """

    trading_date: date
    close: Decimal
    previous_close: Decimal | None
    value: Decimal | None


def simple_return(close, previous_close) -> Decimal | None:
    """The daily simple return ``(close - previous_close) / previous_close``.

    Returns ``None`` when the return is undefined. Those are two different
    situations, and both must be reported as "no value" rather than as a number:

    * **no previous close** -- the first observation of a series, or the first
      observation after a gap the window cannot see past. There is nothing to
      divide by, and answering 0% would claim the price did not move.
    * **a zero previous close** -- a division by zero. A zero close is permitted
      by the table (it constrains ``high >= low`` and ``volume >= 0``, not the
      price), so this is reachable in production data and must neither raise nor
      return ``inf``.

    The arithmetic is ``Decimal`` throughout, and the result is quantized once,
    at the end -- not before dividing, which would compound two roundings.
    """
    if close is None or previous_close is None:
        return None

    divisor = Decimal(previous_close)
    if divisor == 0:
        return None

    return quantize_ratio((Decimal(close) - divisor) / divisor)


def daily_returns(
    dates: Sequence[date],
    closes: Sequence[Decimal],
    *,
    previous_closes: Sequence[Decimal | None] | None = None,
) -> list[ReturnPoint]:
    """One :class:`ReturnPoint` per observation, in the order given.

    ``previous_closes`` is how the database's ``LAG(close)`` window function is
    consumed: the caller passes the column it produced instead of letting this
    function shift the closes itself. Both routes compute the same thing -- one
    shifts in SQL, the other shifts a list that is already in memory -- and
    ``test_daily_returns.py`` asserts they agree row for row, because a silent
    disagreement here would be a wrong number in a report.

    The list aligns with the input, including any warm-up rows the loader
    prepended: index ``i`` always describes ``dates[i]``. Trimming the warm-up is
    the caller's job, because only the caller knows how many rows it asked for.
    """
    if len(dates) != len(closes):
        raise ValueError("dates and closes must be the same length")
    if previous_closes is not None and len(previous_closes) != len(closes):
        raise ValueError("previous_closes must be the same length as closes")

    points: list[ReturnPoint] = []
    for index, (trading_date, close) in enumerate(zip(dates, closes)):
        if previous_closes is None:
            previous = closes[index - 1] if index else None
        else:
            previous = previous_closes[index]

        points.append(
            ReturnPoint(
                trading_date=trading_date,
                close=Decimal(close),
                previous_close=None if previous is None else Decimal(previous),
                value=simple_return(close, previous),
            )
        )
    return points


def daily_returns_pandas(
    dates: Sequence[date],
    closes: Sequence[Decimal],
) -> list[ReturnPoint]:
    """The same result as :func:`daily_returns`, computed by pandas/numpy.

    Two implementation notes, both deliberate:

    * **The shift and the division are written out** rather than using
      ``Series.pct_change()``. ``pct_change`` computes ``close / previous - 1``,
      which is equal to ``(close - previous) / previous`` in exact arithmetic
      but differs in the last unit in the last place of a float64. That
      difference is invisible after quantizing to eight places, and the point of
      this function is to be the reference the SQL path is checked against, so
      it computes the documented formula literally. (``pct_change`` also had its
      ``fill_method`` default change under us between pandas 2.1 and 2.2, which
      is a reason to prefer an explicit shift in code that must not silently
      change what it does.)
    * **pandas and numpy are imported inside the function.** They cost roughly
      half a second to import, and the request path does not need them, so a
      worker that only ever answers from the SQL backend should not pay for
      them at startup.

    A non-finite result -- ``inf`` from a zero previous close, ``nan`` from
    ``0/0`` -- is mapped to ``None``, matching :func:`simple_return`. numpy's
    divide-by-zero warning is suppressed because it is an expected input, not an
    anomaly, and the value is handled rather than propagated.
    """
    import numpy as np
    import pandas as pd

    previous = pd.Series([float(value) for value in closes], dtype="float64").shift(1)
    close = pd.Series([float(value) for value in closes], dtype="float64")

    with np.errstate(divide="ignore", invalid="ignore"):
        change = (close - previous) / previous

    points: list[ReturnPoint] = []
    for index, trading_date in enumerate(dates):
        prior = previous.iloc[index]
        ratio = change.iloc[index]

        previous_decimal = None if pd.isna(prior) else Decimal(str(prior))
        value = (
            None
            if ratio is None or pd.isna(ratio) or not math.isfinite(ratio)
            else quantize_ratio(Decimal(str(ratio)))
        )

        points.append(
            ReturnPoint(
                trading_date=trading_date,
                # The close comes from the input, not from the float round trip:
                # a price is exact and there is no reason to pay for converting
                # it to float64 and back.
                close=Decimal(closes[index]),
                previous_close=previous_decimal,
                value=value,
            )
        )
    return points


# --------------------------------------------------------------------------- #
# Cumulative returns
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CumulativePoint:
    """One date, and how far the range has come by then.

    ``growth`` is the wealth relative -- what one unit of currency held at the
    base has become -- and ``value`` is ``growth - 1``. Both are kept because the
    index form is what a person checks (``1.0198``) and the return form is what a
    report quotes (``1.98%``).
    """

    trading_date: date
    close: Decimal
    base_close: Decimal
    growth: Decimal | None
    value: Decimal | None


def cumulative_returns(
    dates: Sequence[date],
    closes: Sequence[Decimal],
) -> list[CumulativePoint]:
    """The range's compounded return up to each observation.

    Definition::

        growth(t) = close(t) / close(first)
        cumulative(t) = growth(t) - 1

    **The base is the first observation of the requested range, and its
    cumulative return is 0.** The obvious alternative -- anchoring on the last
    close *before* the range -- is a different statement: it reports the move
    *into* the range as if it had happened inside it, and it needs a row the
    caller did not ask for. Anchoring inside the range keeps the answer
    self-contained: every value is reproducible from the range's own prices, and
    the leading 0 says where the base is. A caller who wants the move into the
    range can read the first daily return instead, which is exactly what it is.

    Why this is also "the compounded product of the daily returns": the product
    telescopes, ``prod(1 + r_i) = close(t) / close(first)``, exactly. The engine
    divides the two prices rather than multiplying the daily returns because the
    returns it *reports* are rounded to eight places, and compounding ``n`` of
    them accumulates up to ``n x 5e-9`` of rounding. Dividing the closes is exact
    and gives the same number the product would give at full precision. The tests
    compute the product from the reported returns anyway, as the cross-check that
    the two agree.

    A day whose return is undefined -- a zero previous close -- **breaks the
    chain**: every point from that date onwards is ``None``. Skipping the day (as
    if it had returned 0%) or restarting the chain from the next price would both
    produce a number that no price supports, and a wealth index that quietly
    rebases is worse than one that stops.
    """
    if len(dates) != len(closes):
        raise ValueError("dates and closes must be the same length")
    if not closes:
        return []

    base = Decimal(closes[0])
    points: list[CumulativePoint] = []
    broken = False

    for index, (trading_date, close) in enumerate(zip(dates, closes)):
        if index and not broken and simple_return(close, closes[index - 1]) is None:
            broken = True

        if broken:
            growth = None
            value = None
        elif index:
            raw = Decimal(close) / base
            growth = quantize_ratio(raw)
            value = quantize_ratio(raw - 1)
        else:
            # The base is defined even if its price is zero: one unit held there
            # is still one unit. What a zero base price breaks is the *next*
            # return, which is checked above. Quantized like every other ratio so
            # that a report renders ``1.00000000`` rather than ``1``.
            growth = quantize_ratio(Decimal(1))
            value = quantize_ratio(Decimal(0))

        points.append(
            CumulativePoint(
                trading_date=trading_date,
                close=Decimal(close),
                base_close=base,
                growth=growth,
                value=value,
            )
        )
    return points


def cumulative_returns_pandas(
    dates: Sequence[date],
    closes: Sequence[Decimal],
) -> list[CumulativePoint]:
    """The same result, computed by pandas/numpy.

    A vectorised ratio (``close / close[0]``) for the arithmetic, and an explicit
    walk for the break rule: the chain policy is a rule about the *series*, not a
    per-element operation, so it is stated rather than expressed as a mask. The
    walk is over rows already in memory, so it costs nothing that the vectorised
    step saved.
    """
    import pandas as pd

    if not closes:
        return []

    close = pd.Series([float(value) for value in closes], dtype="float64")
    growth = close / close.iloc[0]
    steps = (close - close.shift(1)) / close.shift(1)

    broken_from = None
    for index in range(1, len(close)):
        step = steps.iloc[index]
        if pd.isna(step) or not math.isfinite(step):
            broken_from = index
            break

    points: list[CumulativePoint] = []
    for index, trading_date in enumerate(dates):
        defined = broken_from is None or index < broken_from
        raw = growth.iloc[index]

        if index == 0:
            growth_decimal, value = quantize_ratio(Decimal(1)), quantize_ratio(Decimal(0))
        elif defined and math.isfinite(raw):
            growth_decimal = quantize_ratio(Decimal(str(raw)))
            value = quantize_ratio(Decimal(str(raw)) - 1)
        else:
            growth_decimal, value = None, None

        points.append(
            CumulativePoint(
                trading_date=trading_date,
                close=Decimal(closes[index]),
                base_close=Decimal(closes[0]),
                growth=growth_decimal,
                value=value,
            )
        )
    return points


# --------------------------------------------------------------------------- #
# Moving averages
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MovingAveragePoint:
    """One date, the mean of the trailing window, and how full that window was."""

    trading_date: date
    close: Decimal
    value: Decimal | None
    window: int
    sample_size: int


def moving_average(
    dates: Sequence[date],
    closes: Sequence[Decimal],
    window: int,
    *,
    values: Sequence[Decimal | None] | None = None,
) -> list[MovingAveragePoint]:
    """The arithmetic mean of the trailing ``window`` observations.

    Window semantics, stated once, because "7-day average" is ambiguous and the
    ambiguity is silent:

    * the window is ``window`` **observations** -- rows -- the current one
      included. A trading-day window, not a calendar-day one: a holiday does not
      shorten it, and a gap does not become a member of it. So a "7-day" average
      over a week containing a holiday spans eight calendar days, and that is the
      intended reading;
    * it is **trailing and right-aligned**: ``mean(close(t - window + 1) .. close(t))``,
      so the value at ``t`` uses no price after ``t``. A centred average would put
      a future price into a past value, which is not usable in a report about a
      date;
    * a **full window is required**. Until ``window`` observations exist the value
      is ``None``, never the mean of whatever there was: a three-observation mean
      labelled a seven-observation mean is a different statistic with the same
      name. ``sample_size`` is reported so a caller can see how far along a young
      series is.

    ``values`` lets the caller supply the means from the database -- ``AVG(close)
    OVER (ROWS BETWEEN window - 1 PRECEDING AND CURRENT ROW)`` -- instead of
    computing them here. A database frame averages however many rows it has, so
    the supplied values are subject to the same completeness rule as the computed
    ones (the first ``window - 1`` rows are reported as ``None``), which is what
    keeps the two routes in agreement.

    The running sum is the implementation: the close that entered the window is
    added, the one that left it is subtracted, so the cost is O(observations)
    rather than O(observations x window). That is exact here and would not be with
    binary floats, where a rolling add/subtract drifts; ``Decimal`` addition is
    exact, so the incremental sum is *identical* to re-adding the window, which a
    test asserts.
    """
    if window < 1:
        raise ValueError("window must be at least 1")
    if len(dates) != len(closes):
        raise ValueError("dates and closes must be the same length")
    if values is not None and len(values) != len(closes):
        raise ValueError("values must be the same length as closes")

    points: list[MovingAveragePoint] = []
    running = Decimal(0)
    entering: deque[Decimal] = deque()

    for index, (trading_date, close) in enumerate(zip(dates, closes)):
        price = Decimal(close)
        running += price
        entering.append(price)
        if len(entering) > window:
            running -= entering.popleft()

        sample_size = min(index + 1, window)
        if sample_size < window:
            value = None
        elif values is not None:
            value = None if values[index] is None else quantize_price(Decimal(values[index]))
        else:
            value = quantize_price(running / window)

        points.append(
            MovingAveragePoint(
                trading_date=trading_date,
                close=price,
                value=value,
                window=window,
                sample_size=sample_size,
            )
        )
    return points


def moving_average_pandas(
    dates: Sequence[date],
    closes: Sequence[Decimal],
    window: int,
) -> list[MovingAveragePoint]:
    """The same series through ``Series.rolling(window).mean()``."""
    import pandas as pd

    if window < 1:
        raise ValueError("window must be at least 1")

    close = pd.Series([float(value) for value in closes], dtype="float64")
    # min_periods defaults to the window, which is the completeness rule above.
    average = close.rolling(window).mean()

    points: list[MovingAveragePoint] = []
    for index, trading_date in enumerate(dates):
        value = average.iloc[index]
        points.append(
            MovingAveragePoint(
                trading_date=trading_date,
                close=Decimal(closes[index]),
                value=None if pd.isna(value) else quantize_price(Decimal(str(value))),
                window=window,
                sample_size=min(index + 1, window),
            )
        )
    return points


# --------------------------------------------------------------------------- #
# Rolling volatility
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VolatilityPoint:
    """One date, the standard deviation of the returns ending there, and the
    working that produced it."""

    trading_date: date
    value: Decimal | None
    daily: Decimal | None
    mean: Decimal | None
    window: int
    sample_size: int


def rolling_volatility(
    dates: Sequence[date],
    returns: Sequence[Decimal | None],
    window: int,
    *,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> list[VolatilityPoint]:
    """The sample standard deviation of the trailing ``window`` daily returns,
    annualized.

    Definition::

        sigma(t)  = sqrt( sum((r_i - mean(r))^2) / (window - 1) )
        value(t)  = sigma(t) * sqrt(periods_per_year)

    The window is given as a **count of returns**, so ``window=20`` is the
    conventional "20-day volatility": twenty daily returns, which needs
    twenty-one closes. A moving-average window of 20 counts twenty *prices*. The
    engine keeps each metric's own standard unit and says which it uses, rather
    than forcing one convention on both and leaving the off-by-one to be
    discovered; ``sample_size`` is in the response so the difference is visible at
    a glance.

    Conventions, each one a choice that another engine could make differently:

    * **sample standard deviation, ``ddof = 1``**, dividing by ``window - 1``. The
      returns in a window are a sample of the process that generated them, not the
      population, and the choice is not cosmetic: with two observations the two
      conventions differ by 41%. This also matches ``pandas.rolling().std()``,
      whose default is ``ddof=1``; the population form is ``ddof=0``.
    * **simple returns**, the same ones :func:`daily_returns` reports, not log
      returns. One definition of "return" in the engine is worth more than the
      small statistical advantage of log returns, which differ at first order for
      small moves and materially for large ones. A log-return variant would change
      this function's input, not its arithmetic.
    * **annualized with ``sqrt(252)``**, 252 sessions a year. This is a model, not
      a fact: it assumes returns are independent and identically distributed, so
      that variance grows linearly with time. Real returns cluster -- a quiet week
      follows a quiet week -- so an annualized figure derived from a quiet window
      overstates the risk and one from a violent window understates it. The
      per-session figure is returned as ``daily`` so a reader can annualize
      differently, or not at all.
    * **a full window of *defined* returns is required.** A zero previous close
      inside the window leaves it incomplete and the value is ``None``. A window
      of identical returns is not incomplete: zero volatility is a value, and it is
      reported as ``0``.

    ``window`` must be at least 2, because a sample standard deviation of one
    observation is a division by zero, not a small number.
    """
    if window < 2:
        raise ValueError("a sample standard deviation needs at least 2 returns")
    if len(dates) != len(returns):
        raise ValueError("dates and returns must be the same length")

    annualizer = Decimal(periods_per_year).sqrt()
    points: list[VolatilityPoint] = []

    for index, trading_date in enumerate(dates):
        start = max(0, index - window + 1)
        sample = returns[start : index + 1]
        defined = [value for value in sample if value is not None]
        sample_size = len(defined)

        if index + 1 < window or sample_size < window:
            points.append(
                VolatilityPoint(
                    trading_date=trading_date,
                    value=None,
                    daily=None,
                    mean=None,
                    window=window,
                    sample_size=sample_size,
                )
            )
            continue

        values = [Decimal(value) for value in defined]
        mean = sum(values, Decimal(0)) / window
        variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / (window - 1)
        sigma = variance.sqrt()

        points.append(
            VolatilityPoint(
                trading_date=trading_date,
                value=quantize_ratio(sigma * annualizer),
                daily=quantize_ratio(sigma),
                mean=quantize_ratio(mean),
                window=window,
                sample_size=window,
            )
        )
    return points


def rolling_volatility_pandas(
    dates: Sequence[date],
    returns: Sequence[Decimal | None],
    window: int,
    *,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> list[VolatilityPoint]:
    """The same series through ``Series.rolling(window).std()`` and ``.mean()``.

    ``rolling().std()`` is already ``ddof=1``, which is the convention this module
    documents; passing its default is the point of comparing against it. An
    undefined return is carried in as ``NaN``, which makes every window that
    contains it ``NaN`` -- the same answer as the ``None`` the decimal
    implementation gives, for the same reason.
    """
    import numpy as np
    import pandas as pd

    if window < 2:
        raise ValueError("a sample standard deviation needs at least 2 returns")

    series = pd.Series(
        [np.nan if value is None else float(value) for value in returns], dtype="float64"
    )
    deviation = series.rolling(window).std()
    mean = series.rolling(window).mean()
    annualizer = math.sqrt(periods_per_year)

    points: list[VolatilityPoint] = []
    for index, trading_date in enumerate(dates):
        # How many defined returns were available for this window, whether or not
        # it was full -- the same number the decimal implementation reports.
        defined = sum(
            1 for value in returns[max(0, index - window + 1) : index + 1] if value is not None
        )
        sigma = deviation.iloc[index]

        if pd.isna(sigma):
            points.append(
                VolatilityPoint(
                    trading_date=trading_date,
                    value=None,
                    daily=None,
                    mean=None,
                    window=window,
                    sample_size=defined,
                )
            )
            continue

        points.append(
            VolatilityPoint(
                trading_date=trading_date,
                value=quantize_ratio(Decimal(str(sigma * annualizer))),
                daily=quantize_ratio(Decimal(str(sigma))),
                mean=quantize_ratio(Decimal(str(mean.iloc[index]))),
                window=window,
                sample_size=window,
            )
        )
    return points


@dataclass(frozen=True)
class DrawdownPoint:
    """One observation, the peak it is measured against, and the decline.

    ``value`` is the drawdown ``close / peak - 1``: ``0`` on a new high and
    negative below one. ``peak`` and ``peak_date`` are carried *per point* rather
    than only in the summary, because "measured from which peak" is the question a
    drawdown raises, and answering it for the worst date alone would leave every
    other row of the response uncheckable.
    """

    trading_date: date
    close: Decimal
    peak: Decimal | None
    peak_date: date | None
    value: Decimal | None


@dataclass(frozen=True)
class DrawdownSummary:
    """The worst peak-to-trough decline in a series, and where it happened.

    ``value`` is signed like every other ratio in the engine: negative for a
    decline, ``0`` for a series that never fell. A report that quotes a drawdown
    as a magnitude reads ``-value``; the engine does not report both, because two
    signs for one statistic is how a number gets inverted.
    """

    value: Decimal | None = None
    peak_date: date | None = None
    trough_date: date | None = None
    peak_close: Decimal | None = None
    trough_close: Decimal | None = None
    recovered: bool | None = None
    recovery_date: date | None = None


def drawdown_series(
    dates: Sequence[date],
    closes: Sequence[Decimal],
    *,
    peaks: Sequence[Decimal | None] | None = None,
) -> list[DrawdownPoint]:
    """The decline from the highest close so far, at every observation.

    Definitions::

        peak(t)   = max(close(s) for s <= t)      # the running maximum, s in range
        value(t)  = close(t) / peak(t) - 1        # <= 0; 0 exactly on a new high

    **The peak is anchored inside the requested range**, at its first
    observation, which is the same decision :func:`cumulative_returns` makes. The
    alternative -- the peak of the instrument's whole history, so that a request
    starting mid-decline reports the decline it inherited -- is a different and
    also legitimate statistic, and it is *not* available from a bounded read: the
    running maximum would have to see every row before ``start_date``, where the
    other metrics read a fixed ``pad``. Anchoring inside the range keeps the read
    one bounded query and makes the answer reproducible from the range's own
    prices, which is the property the rest of the engine is built on. A caller who
    wants a decline that starts earlier requests a range that starts earlier.

    **The peak must be positive for a drawdown to exist.** ``peak = 0`` is
    reachable (a zero close is permitted by the table), and ``close / 0`` is not a
    ratio, so the value is ``None`` there rather than ``0`` or ``inf`` -- the
    engine-wide rule. ``0`` would claim a price equal to its high, and the high is
    not a number this can divide by.

    ``peaks`` lets a caller supply the running maximum computed elsewhere (the
    database, in the ``sql`` backend) instead of scanning for it here. The *date*
    of the peak is derived from the closes in every backend, because the loader
    carries the level of the running maximum and not the row that first reached
    it; the supplied values and the derived date agree because the running maximum
    only moves when a close exceeds it.

    A tie -- a second date reaching the same high -- does not restart the peak:
    the peak date stays at the first date that reached the level, which is what
    makes the reported episode start where a reader would say it started.
    """
    if len(dates) != len(closes):
        raise ValueError("dates and closes must be the same length")
    if peaks is not None and len(peaks) != len(closes):
        raise ValueError("peaks and closes must be the same length")

    points: list[DrawdownPoint] = []
    running_peak: Decimal | None = None
    running_peak_date: date | None = None

    for index, (trading_date, close) in enumerate(zip(dates, closes)):
        close = Decimal(close)

        if running_peak is None or close > running_peak:
            running_peak, running_peak_date = close, trading_date

        supplied = peaks[index] if peaks is not None else None
        peak = running_peak if supplied is None else Decimal(supplied)

        points.append(
            DrawdownPoint(
                trading_date=trading_date,
                close=close,
                peak=peak,
                peak_date=running_peak_date,
                value=None if peak <= 0 else quantize_ratio(close / peak - 1),
            )
        )
    return points


def drawdown_series_pandas(
    dates: Sequence[date],
    closes: Sequence[Decimal],
) -> list[DrawdownPoint]:
    """The same series, with the running maximum taken by ``Series.cummax()``.

    ``cummax`` is the vectorised form of "the highest close so far", and it is the
    only part of a drawdown that is a scan: the ratio, the peak date and the
    summary are then the shared code path, so this backend agrees with the others
    by construction rather than by two implementations happening to match. That
    matters more here than in most places, because a drawdown has no window to
    parameterise -- the whole question is *which* row the maximum came from, which
    is exactly what a cumulative series encodes.
    """
    import pandas as pd

    if len(dates) != len(closes):
        raise ValueError("dates and closes must be the same length")

    peaks = pd.Series([float(value) for value in closes], dtype="float64").cummax()
    return drawdown_series(dates, closes, peaks=[Decimal(str(value)) for value in peaks])


def summarize_drawdown(points: Sequence[DrawdownPoint]) -> DrawdownSummary:
    """The worst drawdown in a series of points, and whether it was made back.

    The summary is derived from the points rather than computed alongside them, so
    every backend reports the same episode by construction -- the alternative is
    three implementations of "which minimum, and was the peak regained", which is
    three chances to disagree about a date.

    Two ties worth naming, because a reader will eventually hit one:

    * **equal-depth episodes** -- the earliest is reported. A drawdown of 20% twice
      is one number; the first occurrence is the one a report means by "the
      drawdown", and it is also the one a recovery search from the later trough
      would mis-date;
    * **a series that never fell** -- ``value`` is ``0`` and the dates are ``None``,
      because there is no peak-to-trough episode to name. ``recovered`` is ``None``
      for the same reason: nothing was given up, so nothing was made back.

    ``recovered`` is scoped to the range: it says the peak was regained *by the
    last observation in the range*, not that it ever will be. A drawdown still
    under water at the end of the range is ``False`` with no ``recovery_date``,
    which is a fact about the data rather than a judgement about the instrument.
    """
    defined = [index for index, point in enumerate(points) if point.value is not None]
    if not defined:
        return DrawdownSummary()

    trough_index = min(defined, key=lambda index: points[index].value)
    worst = points[trough_index]

    if worst.value == 0:
        return DrawdownSummary(value=quantize_ratio(Decimal(0)))

    recovery_date = None
    for point in points[trough_index + 1 :]:
        if worst.peak is not None and point.close >= worst.peak:
            recovery_date = point.trading_date
            break

    return DrawdownSummary(
        value=worst.value,
        peak_date=worst.peak_date,
        trough_date=worst.trading_date,
        peak_close=worst.peak,
        trough_close=worst.close,
        recovered=recovery_date is not None,
        recovery_date=recovery_date,
    )


@dataclass(frozen=True)
class ExtremePoint:
    """The highest and lowest close *so far* in the range, at one date.

    ``is_new_high`` and ``is_new_low`` say whether this row moved the running
    extreme, which is what makes the series checkable by eye: exactly the rows
    where the flag is set are the rows that changed the answer, and the last row's
    ``highest_close``/``lowest_close`` are the range's.
    """

    trading_date: date
    close: Decimal
    highest_close: Decimal
    highest_close_date: date
    lowest_close: Decimal
    lowest_close_date: date
    is_new_high: bool
    is_new_low: bool


@dataclass(frozen=True)
class ExtremesSummary:
    """The highest and lowest prices of a range, and when each occurred.

    Two pairs, because "the highest price" is two different questions and a report
    will ask both:

    * ``highest_close`` / ``lowest_close`` -- the extremes of the closes, the
      reference price every other metric is built on;
    * ``highest_high`` / ``lowest_low`` -- the extremes the market actually
      *touched*, from the ``high`` and ``low`` columns.

    They are not the same number and neither implies the other: a session can print
    a new high without closing at one, and the highest close of a range can be
    lower than a high from an earlier session that gave it all back. The date is
    reported with every extreme, and a tie reports the **first** date that reached
    it -- the same rule the drawdown's peak date follows.
    """

    highest_close: Decimal | None = None
    highest_close_date: date | None = None
    lowest_close: Decimal | None = None
    lowest_close_date: date | None = None
    highest_high: Decimal | None = None
    highest_high_date: date | None = None
    lowest_low: Decimal | None = None
    lowest_low_date: date | None = None
    traded_range: Decimal | None = None


def extreme_series(
    dates: Sequence[date],
    closes: Sequence[Decimal],
    *,
    highest: Sequence[Decimal | None] | None = None,
    lowest: Sequence[Decimal | None] | None = None,
) -> list[ExtremePoint]:
    """The running highest and lowest close, at every observation.

    Definitions::

        highest_close(t) = max(close(s) for s <= t)
        lowest_close(t)  = min(close(s) for s <= t)     # s in the range

    The range is the anchor, as with the drawdown's peak: both extremes start at the
    first requested observation, so the first point is both the highest and the
    lowest close of what has been seen, and ``padding`` is ``0``. The alternative --
    the highest price of the instrument's whole history -- is the same unbounded read
    that metric 5 rejects, and a caller who wants it asks for a longer range.

    A new extreme is a **strict** improvement, so a repeat of the current high does
    not restart its date; the first date that reached the level is the one reported,
    for the same reason as the drawdown's peak.

    ``highest``/``lowest`` let the database supply the running extremes (the ``sql``
    backend) instead of scanning for them here. The dates are derived from the closes
    in every backend, as they are for the drawdown, because the loader carries the
    level of a running extreme and not the row that first reached it.
    """
    if len(dates) != len(closes):
        raise ValueError("dates and closes must be the same length")
    if highest is not None and len(highest) != len(closes):
        raise ValueError("highest and closes must be the same length")
    if lowest is not None and len(lowest) != len(closes):
        raise ValueError("lowest and closes must be the same length")

    points: list[ExtremePoint] = []
    running_high: Decimal | None = None
    running_high_date: date | None = None
    running_low: Decimal | None = None
    running_low_date: date | None = None

    for index, (trading_date, close) in enumerate(zip(dates, closes)):
        close = Decimal(close)

        is_new_high = running_high is None or close > running_high
        if is_new_high:
            running_high, running_high_date = close, trading_date

        is_new_low = running_low is None or close < running_low
        if is_new_low:
            running_low, running_low_date = close, trading_date

        supplied_high = highest[index] if highest is not None else None
        supplied_low = lowest[index] if lowest is not None else None

        points.append(
            ExtremePoint(
                trading_date=trading_date,
                close=close,
                highest_close=running_high if supplied_high is None else Decimal(supplied_high),
                highest_close_date=running_high_date,
                lowest_close=running_low if supplied_low is None else Decimal(supplied_low),
                lowest_close_date=running_low_date,
                is_new_high=is_new_high,
                is_new_low=is_new_low,
            )
        )
    return points


def extreme_series_pandas(
    dates: Sequence[date],
    closes: Sequence[Decimal],
) -> list[ExtremePoint]:
    """The same series, with the running extremes taken by ``cummax``/``cummin``.

    The pair is the vectorised form of a running maximum and minimum, and as with
    the drawdown it is the whole of the scan; the dates, the flags and the summary
    are the shared code path, so the backends agree by construction.
    """
    import pandas as pd

    if len(dates) != len(closes):
        raise ValueError("dates and closes must be the same length")

    series = pd.Series([float(value) for value in closes], dtype="float64")
    return extreme_series(
        dates,
        closes,
        highest=[Decimal(str(value)) for value in series.cummax()],
        lowest=[Decimal(str(value)) for value in series.cummin()],
    )


def _first_extreme(
    dates: Sequence[date],
    values: Sequence[Decimal | None],
    *,
    largest: bool,
) -> tuple[Decimal | None, date | None]:
    """The extreme of a column and the first date that reached it.

    ``max(range(...), key=...)`` returns the *first* position holding the largest
    value, which is the tie rule this engine uses everywhere an extreme has a date:
    the first time it happened.
    """
    defined = [
        (index, Decimal(value)) for index, value in enumerate(values) if value is not None
    ]
    if not defined:
        return None, None

    pick = max if largest else min
    index, chosen = pick(defined, key=lambda pair: pair[1])
    return chosen, dates[index]


def summarize_extremes(
    dates: Sequence[date],
    points: Sequence[ExtremePoint],
    *,
    highs: Sequence[Decimal | None] | None = None,
    lows: Sequence[Decimal | None] | None = None,
) -> ExtremesSummary:
    """The range's extremes, from the running series and the traded columns.

    The close pair comes from the **last point's** running extremes rather than from
    a second pass over the closes: the series and the summary are then the same
    computation seen twice, and a test asserts they equal a direct ``max``/``min``
    rather than assuming it.

    The traded pair comes from the ``high``/``low`` columns, and is ``None`` when they
    were not read -- a close-only series has no traded extremes, and reporting the
    closes under that name would be the worst possible answer.

    ``traded_range`` is the distance between the two traded extremes, and it is **not
    any row's range**: the highest high and the lowest low of a period generally come
    from different sessions, so this is the width of the period rather than of a day.
    """
    if not points:
        return ExtremesSummary()

    last = points[-1]
    summary = ExtremesSummary(
        highest_close=last.highest_close,
        highest_close_date=last.highest_close_date,
        lowest_close=last.lowest_close,
        lowest_close_date=last.lowest_close_date,
    )

    if highs is None or lows is None:
        return summary

    highest_high, highest_high_date = _first_extreme(dates, highs, largest=True)
    lowest_low, lowest_low_date = _first_extreme(dates, lows, largest=False)

    return ExtremesSummary(
        highest_close=summary.highest_close,
        highest_close_date=summary.highest_close_date,
        lowest_close=summary.lowest_close,
        lowest_close_date=summary.lowest_close_date,
        highest_high=highest_high,
        highest_high_date=highest_high_date,
        lowest_low=lowest_low,
        lowest_low_date=lowest_low_date,
        traded_range=(
            None if highest_high is None or lowest_low is None else highest_high - lowest_low
        ),
    )
