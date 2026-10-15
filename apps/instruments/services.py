"""Business logic for instruments and exchanges.

Views parse the request and render the response. Everything shaped like a query
lives here, so the same call is reusable from a Celery task, a management
command, or another service without going through HTTP.
"""

from django.db.models import Q

from .models import Exchange, Instrument


def get_exchange_queryset():
    return Exchange.objects.all()


def get_instrument_queryset():
    """The base queryset for every instrument read.

    ``select_related`` pulls the exchange in the same SQL statement. Without it,
    rendering a page of instruments would issue one query per row for the
    exchange -- the N+1 problem.
    """
    return Instrument.objects.select_related("exchange")


def list_instruments(
    queryset=None,
    *,
    symbol=None,
    exchange_code=None,
    asset_type=None,
    is_active=None,
    search=None,
):
    """Returns instruments matching the given criteria, ordered by symbol.

    Every argument is optional and additive. ``symbol`` and ``exchange_code``
    match case-insensitively and exactly; ``search`` matches a substring of
    either the symbol or the full name.
    """
    queryset = get_instrument_queryset() if queryset is None else queryset

    if symbol:
        queryset = queryset.filter(symbol__iexact=symbol)
    if exchange_code:
        queryset = queryset.filter(exchange__code__iexact=exchange_code)
    if asset_type:
        queryset = queryset.filter(asset_type=asset_type)
    if is_active is not None:
        queryset = queryset.filter(is_active=is_active)
    if search:
        queryset = queryset.filter(
            Q(symbol__icontains=search) | Q(name__icontains=search)
        )

    return queryset.order_by("symbol")


def get_instrument(pk):
    """Returns one instrument with its exchange joined, or raises DoesNotExist."""
    return get_instrument_queryset().get(pk=pk)
