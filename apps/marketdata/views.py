"""Price endpoints: thin adapters over the services module."""

from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.exceptions import ValidationError

from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
)

from apps.instruments.models import Instrument

from . import services
from .models import PriceRecord
from .serializers import PriceRecordSerializer


@extend_schema(
    tags=["Market Data"],
    summary="Price history for an instrument",
    description=(
        "One instrument's stored prices, optionally bounded by an inclusive "
        "`start_date`/`end_date` (ISO `YYYY-MM-DD`). A malformed or inverted "
        "range is a `400`. Order by `trading_date`, `close`, or `volume`."
    ),
    parameters=[
        OpenApiParameter(
            name="start_date",
            type=OpenApiTypes.DATE,
            location=OpenApiParameter.QUERY,
            description="Inclusive lower bound (YYYY-MM-DD).",
        ),
        OpenApiParameter(
            name="end_date",
            type=OpenApiTypes.DATE,
            location=OpenApiParameter.QUERY,
            description="Inclusive upper bound (YYYY-MM-DD).",
        ),
    ],
    responses={200: PriceRecordSerializer(many=True)},
)
class PriceHistoryAPIView(generics.ListAPIView):
    """Lists historical prices for one instrument, optionally within a date range.

    Read-only, so ``ListAPIView``: prices are produced by the ingestion
    pipeline, so there is no create action to expose here.
    """

    #: Empty queryset for DRF's schema introspection; ``get_queryset()`` below
    #: resolves the instrument from the URL and applies the date range.
    queryset = PriceRecord.objects.none()

    serializer_class = PriceRecordSerializer
    ordering_fields = ["trading_date", "close", "volume"]
    ordering = ["trading_date"]

    def get_instrument(self):
        return get_object_or_404(
            Instrument.objects.select_related("exchange"),
            pk=self.kwargs["instrument_pk"],
        )

    def get_queryset(self):
        params = self.request.query_params

        try:
            start_date = services.parse_date(params.get("start_date"))
            end_date = services.parse_date(params.get("end_date"))
            services.validate_date_range(start_date, end_date)
        except ValueError as exc:
            # A malformed or inverted range is the client's mistake, so it is a
            # 400 rather than a 500.
            raise ValidationError({"date_range": [str(exc)]}) from exc

        # Ordering is left to DRF's OrderingFilter, which replaces this default
        # when the client passes ?ordering=.
        return services.get_prices_for_instrument(
            self.get_instrument(), start_date=start_date, end_date=end_date
        )
