"""Analytics endpoints.

Two reads over the same engine, and neither writes. ``AnalyticSnapshotListAPIView``
serves *stored* metric rows; :class:`MetricSeriesAPIView` and
:class:`RankingAPIView` serve the engine's own computation, called directly. The
computed pair is deliberately uncached: their answers depend on price rows, whose
writes do not bump the analytics cache namespace, so caching them would risk
serving a series computed before the newest session. See ``docs/market_analytics.md``.
"""

import json
from datetime import date

from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.common import cache
from apps.common.views import CachedReadMixin
from apps.instruments.models import Instrument

from . import services
from .metrics import MOVING_AVERAGE_NAMES, RANKING_MEASURES, SelectedPeriod
from .serializers import (
    AnalyticSnapshotSerializer,
    CumulativeReturnsSerializer,
    DailyReturnsSerializer,
    MaximumDrawdownSerializer,
    MovingAveragesSerializer,
    PercentageChangeSerializer,
    PriceExtremesSerializer,
    RankingSerializer,
    RollingVolatilitySerializer,
    VolumeSummariesSerializer,
)


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


# --------------------------------------------------------------------------- #
# The computed metrics
#
# The engine is a library, so these endpoints parse a request, call exactly one
# service, and serialize the result. No arithmetic, no second implementation, and
# no query construction: if a number is wrong it is wrong in one place, and the
# test for it lives with that place.
# --------------------------------------------------------------------------- #

#: The metric a request gets when it does not name one.
DEFAULT_METRIC = "returns"

#: The closed metric vocabulary, mapped to the service that computes each one. A
#: name is absent from this table or it cannot be asked for; there is no fallback
#: for an unknown name, because a typo silently answering a different question is
#: the worst kind of default.
METRIC_SERVICES = {
    "returns": services.daily_returns_for_instrument,
    "cumulative_returns": services.cumulative_returns_for_instrument,
    "moving_averages": services.moving_averages_for_instrument,
    "volatility": services.rolling_volatility_for_instrument,
    "drawdown": services.maximum_drawdown_for_instrument,
    "extremes": services.price_extremes_for_instrument,
    "volume": services.volume_summaries_for_instrument,
    "percentage_change": services.percentage_change_for_instrument,
}

METRIC_SERIALIZERS = {
    "returns": DailyReturnsSerializer,
    "cumulative_returns": CumulativeReturnsSerializer,
    "moving_averages": MovingAveragesSerializer,
    "volatility": RollingVolatilitySerializer,
    "drawdown": MaximumDrawdownSerializer,
    "extremes": PriceExtremesSerializer,
    "volume": VolumeSummariesSerializer,
    "percentage_change": PercentageChangeSerializer,
}

#: Parameters every metric takes. ``format`` is here because DRF reads it for
#: content negotiation, not because a metric does anything with it.
SHARED_PARAMS = ("metric", "start_date", "end_date", "backend", "format")

#: The parameters a metric takes *besides* the shared ones. A metric absent from
#: this table takes none, so asking it for anything else is an error rather than a
#: silently ignored no-op.
EXTRA_PARAMS = {
    "moving_averages": ("names",),
    "volatility": ("window",),
    "percentage_change": ("periods",),
}

#: The volatility window when the request does not give one: a month of sessions,
#: the convention the metric's own documentation uses.
DEFAULT_VOLATILITY_WINDOW = 30

#: A ranking's parameters, and its default measure.
RANKING_PARAMS = (
    "measure",
    "instrument_ids",
    "start_date",
    "end_date",
    "lookback",
    "descending",
    "limit",
    "backend",
    "format",
)
DEFAULT_MEASURE = "return"


class QueryParameterMixin:
    """Parses the query parameters the computed endpoints accept.

    Parsing lives here rather than in a service: a service's contract is Python
    types and arithmetic, and a malformed request is an HTTP concern. Every failure
    is raised as a DRF ``ValidationError`` under the parameter's own name, so it
    comes back as a 400 in the same ``{"error": {"fields": ...}}`` shape the rest of
    the API already uses.
    """

    def raw(self, name):
        """The parameter's value, with an empty string treated as absent.

        An omitted parameter and ``?window=`` are the same thing to a caller who
        built a URL from form fields, and both mean "use the default".
        """
        value = self.request.query_params.get(name)
        if value is None or value.strip() == "":
            return None
        return value

    def present(self, name):
        return self.raw(name) is not None

    def parse_date(self, name):
        value = self.raw(name)
        if value is None:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(
                {name: [f"'{value}' is not a valid ISO date (YYYY-MM-DD)."]}
            ) from exc

    def parse_int(self, name, *, default, minimum=None):
        value = self.raw(name)
        if value is None:
            return default
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValidationError({name: [f"'{value}' is not an integer."]}) from exc
        if minimum is not None and parsed < minimum:
            raise ValidationError({name: [f"Must be at least {minimum}."]})
        return parsed

    def parse_bool(self, name, *, default):
        value = self.raw(name)
        if value is None:
            return default
        lowered = value.lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        raise ValidationError(
            {name: [f"'{value}' is not a boolean; use true or false."]}
        )

    def parse_choice(self, name, *, default, choices):
        value = self.raw(name)
        if value is None:
            return default
        if value not in choices:
            raise ValidationError(
                {name: [f"'{value}' is not valid; choose one of {list(choices)}."]}
            )
        return value

    def reject_unknown(self, allowed):
        """Rejects a parameter the chosen shape does not take.

        Silently ignoring an inapplicable parameter would let a caller believe they
        had asked for something. ``?window=30`` on the moving averages answers about
        three fixed windows regardless, so it is a 400 naming what is accepted --
        the same reason an unknown metric is a 400 rather than a default.
        """
        unknown = sorted(
            name for name in self.request.query_params if name not in allowed
        )
        if unknown:
            accepted = ", ".join(sorted(allowed))
            raise ValidationError(
                {
                    "query_params": [
                        f"{', '.join(unknown)} not accepted here; accepted: {accepted}."
                    ]
                }
            )


class MetricSeriesAPIView(QueryParameterMixin, generics.GenericAPIView):
    """Computes one metric for one instrument, over an optional date range.

    ``metric`` names which one (default ``returns``) and the remaining parameters
    are that metric's own inputs. There is no pagination on purpose: a windowed
    metric cannot be paged without recomputing its warm-up for every page, and the
    envelope -- ``padding``, ``window``, ``undefined`` -- describes the series as a
    whole. The payload is therefore one object whose ``points`` are the series.

    Defaults are the engine's, not the endpoint's: an omitted ``start_date`` or
    ``end_date`` is no bound, an omitted ``backend`` is the backend that exists for
    that metric, an omitted ``window`` is :data:`DEFAULT_VOLATILITY_WINDOW`, an
    omitted ``names`` is all three conventional averages, and an omitted ``periods``
    is a single period spanning the requested range.
    """

    pagination_class = None
    serializer_class = DailyReturnsSerializer

    def get_instrument(self):
        return get_object_or_404(
            Instrument.objects.select_related("exchange"),
            pk=self.kwargs["instrument_pk"],
        )

    def get(self, request, *args, **kwargs):
        metric = self.raw("metric") or DEFAULT_METRIC
        if metric not in METRIC_SERVICES:
            choices = ", ".join(sorted(METRIC_SERVICES))
            raise ValidationError(
                {"metric": [f"'{metric}' is not a metric; choose one of {choices}."]}
            )
        self.reject_unknown(SHARED_PARAMS + EXTRA_PARAMS.get(metric, ()))

        try:
            result = METRIC_SERVICES[metric](
                self.get_instrument(), **self.service_kwargs(metric)
            )
        except ValueError as exc:
            # Every ValueError the engine raises is about the request -- an unknown
            # backend, an inverted range, a window too small, a period passed
            # alongside the dates it excludes. That is the client's mistake, so 400.
            raise ValidationError({"metric": [str(exc)]}) from exc

        return Response(METRIC_SERIALIZERS[metric](result).data)

    def service_kwargs(self, metric):
        kwargs = {
            "start_date": self.parse_date("start_date"),
            "end_date": self.parse_date("end_date"),
        }
        # Left out entirely when not asked for, so the service's own default -- the
        # backend that exists for that metric -- is what applies.
        if self.present("backend"):
            kwargs["backend"] = self.raw("backend")

        if metric == "moving_averages" and self.present("names"):
            kwargs["names"] = self.parse_names()
        if metric == "volatility":
            kwargs["window"] = self.parse_int(
                "window", default=DEFAULT_VOLATILITY_WINDOW, minimum=2
            )
        if metric == "percentage_change" and self.present("periods"):
            kwargs["periods"] = self.parse_periods()
        return kwargs

    def parse_names(self):
        """The moving-average series to compute, from the engine's vocabulary.

        The names are the engine's own (``moving_average_7`` and so on) rather than
        a bare window: a second spelling for the same series would be a second
        vocabulary to keep in step.
        """
        names = [part.strip() for part in self.raw("names").split(",") if part.strip()]
        unknown = [name for name in names if name not in MOVING_AVERAGE_NAMES]
        if unknown:
            choices = ", ".join(MOVING_AVERAGE_NAMES)
            raise ValidationError(
                {"names": [f"{', '.join(unknown)} not known; choose from {choices}."]}
            )
        return names

    def parse_periods(self):
        """The multi-period form of the change, as one JSON array in the URL.

        A GET has no body and a list has no natural flat spelling, so the periods
        are a single JSON value: ``[{"label": "q1", "start_date": "...",
        "end_date": "..."}, ...]``. ``label`` is optional and comes back on the
        answer; the two dates are not.
        """
        try:
            loaded = json.loads(self.raw("periods"))
        except json.JSONDecodeError as exc:
            raise ValidationError(
                {"periods": ["Must be a JSON array of periods."]}
            ) from exc
        if not isinstance(loaded, list):
            raise ValidationError({"periods": ["Must be a JSON array of periods."]})

        periods = []
        for index, item in enumerate(loaded):
            if not isinstance(item, dict):
                raise ValidationError({"periods": [f"Entry {index} is not an object."]})
            try:
                periods.append(
                    SelectedPeriod(
                        label=item.get("label"),
                        start_date=date.fromisoformat(item["start_date"]),
                        end_date=date.fromisoformat(item["end_date"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError(
                    {"periods": [f"Entry {index} needs ISO start_date and end_date."]}
                ) from exc
        return tuple(periods)


class RankingAPIView(QueryParameterMixin, generics.GenericAPIView):
    """Ranks instruments by one measure over one window.

    Cross-sectional, so no instrument appears in the path: the answer is *about*
    symbols rather than belonging to one. ``measure`` is the closed vocabulary
    ``return`` / ``volume`` (default ``return``). ``limit`` truncates after ranking,
    so the rows that come back keep their real places rather than being renumbered
    as a page.

    Defaults: an omitted ``lookback`` is the whole bounded range, an omitted
    ``descending`` is best-first, and an omitted ``limit`` returns every instrument
    in the universe.
    """

    pagination_class = None
    serializer_class = RankingSerializer

    def get(self, request, *args, **kwargs):
        self.reject_unknown(RANKING_PARAMS)
        try:
            ranking = services.ranking_for_instruments(**self.service_kwargs())
        except ValueError as exc:
            raise ValidationError({"ranking": [str(exc)]}) from exc

        return Response(RankingSerializer(ranking).data)

    def service_kwargs(self):
        kwargs = {
            "measure": self.parse_choice(
                "measure", default=DEFAULT_MEASURE, choices=tuple(RANKING_MEASURES)
            ),
            "start_date": self.parse_date("start_date"),
            "end_date": self.parse_date("end_date"),
            "lookback": self.parse_int("lookback", default=None, minimum=1),
            "descending": self.parse_bool("descending", default=True),
            "limit": self.parse_int("limit", default=None, minimum=0),
        }
        if self.present("instrument_ids"):
            kwargs["instrument_ids"] = self.parse_instrument_ids()
        if self.present("backend"):
            kwargs["backend"] = self.raw("backend")
        return kwargs

    def parse_instrument_ids(self):
        raw = [
            part.strip()
            for part in self.raw("instrument_ids").split(",")
            if part.strip()
        ]
        ids = []
        for part in raw:
            try:
                ids.append(int(part))
            except ValueError as exc:
                raise ValidationError(
                    {"instrument_ids": [f"'{part}' is not an instrument id."]}
                ) from exc
        return ids
