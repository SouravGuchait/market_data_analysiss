"""Analytics endpoints."""

from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.exceptions import ValidationError

from apps.instruments.models import Instrument

from . import services
from .serializers import AnalyticSnapshotSerializer


class AnalyticSnapshotListAPIView(generics.ListAPIView):
    """Lists the analytical metrics recorded for one instrument.

    Read-only, so ``ListAPIView``: metrics are computed by the pipeline and
    stored, never supplied by a client.
    """

    serializer_class = AnalyticSnapshotSerializer
    ordering_fields = ["as_of_date", "metric_type", "value"]
    ordering = ["-as_of_date"]

    def get_queryset(self):
        instrument = get_object_or_404(
            Instrument.objects.select_related("exchange"), pk=self.kwargs["instrument_pk"]
        )
        params = self.request.query_params

        start_date = self._parse_date(params.get("start_date"))
        end_date = self._parse_date(params.get("end_date"))

        try:
            if start_date and end_date and start_date > end_date:
                raise ValueError("start_date must not be after end_date.")
        except ValueError as exc:
            raise ValidationError({"date_range": [str(exc)]}) from exc

        # Ordering is left to DRF's OrderingFilter.
        return services.list_snapshots_for_instrument(
            instrument,
            metric_type=params.get("metric_type"),
            start_date=start_date,
            end_date=end_date,
        )

    @staticmethod
    def _parse_date(value):
        if not value:
            return None
        from datetime import date

        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(
                {"date_range": [f"'{value}' is not a valid ISO date."]}
            ) from exc
