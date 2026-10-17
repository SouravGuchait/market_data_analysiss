"""Analytics endpoints."""

from datetime import date

from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.exceptions import ValidationError

from apps.common import cache
from apps.common.views import CachedReadMixin
from apps.instruments.models import Instrument

from . import services
from .serializers import AnalyticSnapshotSerializer


class AnalyticSnapshotListAPIView(CachedReadMixin, generics.ListAPIView):
    """Lists the analytical metrics recorded for one instrument.

    Read-only, so ``ListAPIView``: metrics are computed by the pipeline and
    stored, never supplied by a client.

    This is the hot read in the API -- 2,000 snapshots per instrument in the
    benchmark dataset, and the default page (no metric filter) is the most
    expensive shape of it. Its answer only changes when something writes a
    snapshot, which bumps the namespace version, so the response is cached
    read-through. See :mod:`apps.common.cache`.
    """

    serializer_class = AnalyticSnapshotSerializer
    ordering_fields = ["as_of_date", "metric_type", "value"]
    ordering = ["-as_of_date"]

    #: The namespace whose version invalidates every page cached here.
    cache_namespace = "analytics"
    cache_ttl_setting = "API_CACHE_ANALYTICS_TTL"
    cache_ttl_default = 300

    def get_cache_ttl(self):
        return self.setting(self.cache_ttl_setting, self.cache_ttl_default)

    # ----------------------------------------------------------------- #
    # Request dimensions
    # ----------------------------------------------------------------- #

    def get_filters(self):
        """The request's dimensions, parsed once and without touching the database.

        The queryset and the cache key are both built from this one dict, so a
        filter that is missing from the key is a filter that is missing from the
        query. The two cannot silently drift apart.
        """
        params = self.request.query_params
        start_date = self._parse_date(params.get("start_date"))
        end_date = self._parse_date(params.get("end_date"))

        if start_date and end_date and start_date > end_date:
            raise ValidationError(
                {"date_range": ["start_date must not be after end_date."]}
            )

        return {
            "metric_type": params.get("metric_type") or None,
            # Lookback is part of a metric's configuration: a 30-day moving
            # average and a 90-day one are different series with the same name.
            # Filtering it in SQL keeps both the row count and the payload down.
            "lookback_days": self._parse_lookback(params.get("lookback_days")),
            "start_date": start_date,
            "end_date": end_date,
        }

    def get_cache_key(self):
        """The cache key for this page: namespace version, scope, and dimensions.

        Scope is the caller's *role*, not their user id. Snapshots are reference
        data and every authenticated role currently sees the same rows, so keying
        by user id would multiply identical copies for no isolation. The role is
        recorded anyway so that widening the filter to a role -- which is exactly
        the change this endpoint would see -- cannot begin serving one role a page
        obtained by another.

        The host is part of the key because ``next`` and ``previous`` are absolute
        URLs: a payload cached behind one host must not be handed out as another
        host's links.
        """
        filters = self.get_filters()
        params = self.request.query_params

        return cache.build_key(
            self.cache_namespace,
            parts={
                "instrument": self.kwargs["instrument_pk"],
                "metric_type": filters["metric_type"],
                "lookback_days": filters["lookback_days"],
                "from": filters["start_date"],
                "to": filters["end_date"],
                "ordering": params.get("ordering") or self.ordering[0],
                "page": params.get("page"),
                "page_size": params.get("page_size"),
                "host": self.request.get_host(),
            },
            scope=f"role:{getattr(self.request.user, 'role', 'unknown')}",
        )

    def list(self, request, *args, **kwargs):
        return self.cached_response(request, *args, **kwargs)

    def produce_payload(self, request, *args, **kwargs):
        """The uncached page, in the exact shape the client would have received.

        Caching the rendered representation rather than the queryset is what makes
        a cached body and a fresh body identical: a ``Decimal`` is already the
        string DRF would have emitted, and a date is already its ISO form. A test
        asserts the two are byte for byte equal.

        ``super().list`` reaches DRF's implementation because ``CachedReadMixin``
        deliberately defines no ``list`` of its own -- if it did, this call would
        re-enter the cache.
        """
        return super().list(request, *args, **kwargs).data

    # ----------------------------------------------------------------- #
    # Queryset
    # ----------------------------------------------------------------- #

    def get_queryset(self):
        instrument = get_object_or_404(
            Instrument.objects.select_related("exchange"), pk=self.kwargs["instrument_pk"]
        )
        filters = self.get_filters()

        # Ordering is left to DRF's OrderingFilter.
        return services.list_snapshots_for_instrument(
            instrument,
            metric_type=filters["metric_type"],
            start_date=filters["start_date"],
            end_date=filters["end_date"],
            lookback_days=filters["lookback_days"],
        )

    # ----------------------------------------------------------------- #
    # Parsing
    # ----------------------------------------------------------------- #

    @staticmethod
    def _parse_date(value):
        if not value:
            return None

        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(
                {"date_range": [f"'{value}' is not a valid ISO date."]}
            ) from exc

    @staticmethod
    def _parse_lookback(value):
        if value in (None, ""):
            return None

        try:
            lookback = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                {"lookback_days": [f"'{value}' is not an integer."]}
            ) from exc

        if lookback < 0:
            raise ValidationError({"lookback_days": ["Must not be negative."]})
        return lookback
