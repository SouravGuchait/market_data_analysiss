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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

#: The quantum a ratio is rounded to, and its number of decimal places.
#: Kept in step with ``AnalyticSnapshot.value`` -- see the module docstring.
RATIO_EXPONENT = Decimal("0.00000001")
RATIO_DECIMAL_PLACES = 8


def quantize_ratio(value: Decimal) -> Decimal:
    """Rounds a ratio to the engine's reporting precision.

    ``ROUND_HALF_UP`` rather than Python's default banker's rounding: this value
    ends up in a report, and "half rounds away from zero" is what a reader of a
    percentage expects. It is symmetric about zero, so a gain and an equal loss
    round to the same magnitude.
    """
    return value.quantize(RATIO_EXPONENT, rounding=ROUND_HALF_UP)


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
