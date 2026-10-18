"""Business logic for the public API facade.

This app adds no models. Every function here composes the domain apps' own
services -- it reads their querysets and calls their aggregations -- so the
public surface and the domain surface can never drift into two implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from apps.analytics.series import calendar_sessions, missing_trading_dates
from apps.marketdata import services as price_services

#: Coverage is a ratio, reported at the eight places every other ratio in the API
#: uses, so a payload does not depend on which database answered it.
COVERAGE_PLACES = Decimal("0.00000001")


@dataclass(frozen=True)
class InstrumentQuality:
    """What a data-quality review of one instrument's window shows.

    ``sessions`` is how many trading sessions the calendar says the exchange was
    open for; ``missing_dates`` are the ones this instrument has no price for.
    The two together are the whole story: ``sessions - len(missing_dates)`` is the
    covered count, which :attr:`coverage` expresses as a ratio.
    """

    symbol: str
    start_date: date | None
    end_date: date | None
    observations: int
    sessions: int
    missing_dates: tuple[date, ...]
    first_date: date | None
    last_date: date | None

    @property
    def missing_sessions(self) -> int:
        return len(self.missing_dates)

    @property
    def coverage(self) -> Decimal | None:
        """Priced sessions over expected sessions, or ``None`` without a calendar."""
        if not self.sessions:
            return None
        covered = self.sessions - self.missing_sessions
        return (Decimal(covered) / Decimal(self.sessions)).quantize(COVERAGE_PLACES)


def instrument_quality(
    instrument, *, start_date: date | None = None, end_date: date | None = None
) -> InstrumentQuality:
    """Assesses one instrument's price coverage over a date range.

    An omitted bound falls back to the instrument's own first/last price, so a
    request without a range still reviews everything on file. An instrument with
    no prices at all has no window to review, and reports zeros rather than an
    error: "no data" is a quality finding, not a bad request.

    Cost is a small fixed number of queries regardless of range length: one
    aggregate for the counts and bounds, one for the sessions, and one for the
    gaps.
    """
    priced = price_services.get_price_list_queryset().filter(instrument=instrument)
    if start_date:
        priced = priced.filter(trading_date__gte=start_date)
    if end_date:
        priced = priced.filter(trading_date__lte=end_date)
    summary = price_services.get_price_summary(priced)

    start = start_date or summary["first_trading_date"]
    end = end_date or summary["last_trading_date"]

    if start and end:
        missing = tuple(missing_trading_dates(instrument, start, end))
        sessions = calendar_sessions(instrument, start, end).count()
    else:
        missing = ()
        sessions = 0

    return InstrumentQuality(
        symbol=instrument.symbol,
        start_date=start,
        end_date=end,
        observations=summary["record_count"] or 0,
        sessions=sessions,
        missing_dates=missing,
        first_date=summary["first_trading_date"],
        last_date=summary["last_trading_date"],
    )
