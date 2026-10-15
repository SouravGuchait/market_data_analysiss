"""Business logic for price records and the trading calendar."""

from datetime import date, timedelta

from django.db.models import Count, Max, Min

from .models import PriceRecord


def get_price_queryset():
    """The base queryset for every price read.

    ``select_related`` joins the instrument and its exchange in one statement.
    Without it a price page would issue one query per row for the symbol.
    """
    return PriceRecord.objects.select_related("instrument", "instrument__exchange")


def get_prices_for_instrument(
    instrument,
    *,
    start_date=None,
    end_date=None,
    ascending=True,
):
    """Returns price records for one instrument, oldest first by default.

    Both date bounds are inclusive. Either may be omitted, in which case it is
    unbounded. Returns a queryset so callers can slice or paginate it.
    """
    queryset = get_price_queryset().filter(instrument=instrument)

    if start_date:
        queryset = queryset.filter(trading_date__gte=start_date)
    if end_date:
        queryset = queryset.filter(trading_date__lte=end_date)

    ordering = "trading_date" if ascending else "-trading_date"
    return queryset.order_by(ordering)


def parse_date(value):
    """Parses an ISO date string, or returns None for an empty value."""
    if value in (None, ""):
        return None
    return date.fromisoformat(value)


def validate_date_range(start_date, end_date):
    """Raises ValueError if the range is inverted, else returns it."""
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date.")
    return start_date, end_date


def get_price_summary(prices):
    """Aggregates a price queryset into a summary dictionary.

    Aggregated in SQL rather than Python so the database does the work.
    """
    return prices.aggregate(
        first_trading_date=Min("trading_date"),
        last_trading_date=Max("trading_date"),
        record_count=Count("id"),
    )


def get_default_range(instrument, *, days=90):
    """Returns the most recent ``days`` of prices when no range is given.

    Falls back to the instrument's actual span if it has less history than the
    requested window, so an empty response is never produced by accident.
    """
    bounds = get_price_queryset().filter(instrument=instrument).aggregate(
        earliest=Min("trading_date"), latest=Max("trading_date")
    )
    if bounds["latest"] is None:
        return None, None
    start = bounds["latest"] - timedelta(days=days)
    return max(start, bounds["earliest"]), bounds["latest"]
