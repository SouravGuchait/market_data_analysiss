"""Instrument endpoints: thin adapters over the services module."""
from rest_framework import generics

from . import services
from .filters import InstrumentFilter
from .serializers import (
    ExchangeSerializer,
    InstrumentDetailSerializer,
    InstrumentListSerializer,
)


class InstrumentListAPIView(generics.ListAPIView):
    """Lists instruments, filterable by symbol, exchange, and asset type.

    Read-only, so ``ListAPIView`` rather than a ``ModelViewSet``: it declares
    exactly the one action this resource exposes and nothing more.
    """
    serializer_class = InstrumentListSerializer
    filterset_class = InstrumentFilter
    search_fields = ["symbol", "name"]
    ordering_fields = ["symbol", "name", "asset_type", "created_at"]
    ordering = ["symbol"]

    def get_queryset(self):
        # The service owns the select_related, so the exchange is always joined.
        return services.get_instrument_queryset()


class InstrumentRetrieveAPIView(generics.RetrieveAPIView):
    """Retrieves a single instrument with its exchange nested."""

    serializer_class = InstrumentDetailSerializer

    def get_queryset(self):
        return services.get_instrument_queryset()


class ExchangeListAPIView(generics.ListAPIView):
    """Lists exchanges.

    Unpaginated on purpose: the table holds a few hundred rows at most, so a
    page wrapper would add noise without saving anything.
    """
    serializer_class = ExchangeSerializer
    pagination_class = None

    def get_queryset(self):
        return services.get_exchange_queryset()
