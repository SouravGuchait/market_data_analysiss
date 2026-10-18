"""Instrument endpoints: thin adapters over the services module."""
from urllib.parse import urlencode
from drf_spectacular.utils import extend_schema
from rest_framework import generics

from apps.common import cache
from apps.common.views import CachedReadMixin

from . import services
from .filters import InstrumentFilter
from .serializers import (
    ExchangeSerializer,
    InstrumentDetailSerializer,
    InstrumentListSerializer,
)


@extend_schema(
    tags=["Instruments"],
    summary="List instruments",
    description=(
        "Filter by `symbol`, `exchange`, `asset_type`, and `is_active`; search "
        "`symbol` or `name`; order by `symbol`, `name`, `asset_type`, or "
        "`created_at`. Warning: `is_active` accepts only `true`/`false` -- any "
        "other value is silently ignored."
    ),
)
class InstrumentListAPIView(CachedReadMixin, generics.ListAPIView):
    """Lists instruments, filterable by symbol, exchange, and asset type.

    Read-only, so ``ListAPIView`` rather than a ``ModelViewSet``: it declares
    exactly the one action this resource exposes and nothing more.
    """
    serializer_class = InstrumentListSerializer
    filterset_class = InstrumentFilter
    search_fields = ["symbol", "name"]
    ordering_fields = ["symbol", "name", "asset_type", "created_at"]
    ordering = ["symbol"]

    cache_namespace = "reference"

    def get_cache_ttl(self):
        return self.setting("API_CACHE_REFERENCE_TTL", 300)

    def get_cache_key(self):
        return cache.build_key(
            self.cache_namespace,
            parts={
                "resource": "instruments",
                "query": urlencode(sorted(self.request.query_params.lists()), doseq=True),
                "host": self.request.get_host(),
            },
        )

    def list(self, request, *args, **kwargs):
        return self.cached_response(request, *args, **kwargs)

    def produce_payload(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs).data

    def get_queryset(self):
        # The service owns the select_related, so the exchange is always joined.
        return services.get_instrument_queryset()


@extend_schema(
    tags=["Instruments"],
    summary="Retrieve an instrument",
    description="One instrument by primary key, with its exchange nested.",
)
class InstrumentRetrieveAPIView(CachedReadMixin, generics.RetrieveAPIView):
    """Retrieves a single instrument with its exchange nested."""

    serializer_class = InstrumentDetailSerializer

    cache_namespace = "reference"

    def get_cache_ttl(self):
        return self.setting("API_CACHE_REFERENCE_TTL", 300)

    def get_cache_key(self):
        return cache.build_key(
            self.cache_namespace,
            parts={
                "resource": "instrument",
                "id": self.kwargs["pk"],
                "host": self.request.get_host(),
            },
        )

    def retrieve(self, request, *args, **kwargs):
        return self.cached_response(request, *args, **kwargs)

    def produce_payload(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs).data

    def get_queryset(self):
        return services.get_instrument_queryset()


@extend_schema(
    tags=["Instruments"],
    summary="List exchanges",
    description="All exchanges. Deliberately unpaginated.",
)
class ExchangeListAPIView(CachedReadMixin, generics.ListAPIView):
    """Lists exchanges.

    Unpaginated on purpose: the table holds a few hundred rows at most, so a
    page wrapper would add noise without saving anything.
    """
    serializer_class = ExchangeSerializer
    pagination_class = None

    cache_namespace = "reference"

    def get_cache_ttl(self):
        return self.setting("API_CACHE_REFERENCE_TTL", 300)

    def get_cache_key(self):
        return cache.build_key(
            self.cache_namespace,
            parts={"resource": "exchanges", "host": self.request.get_host()},
        )

    def list(self, request, *args, **kwargs):
        return self.cached_response(request, *args, **kwargs)

    def produce_payload(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs).data

    def get_queryset(self):
        return services.get_exchange_queryset()
