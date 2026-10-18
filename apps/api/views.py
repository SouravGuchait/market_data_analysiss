"""The public API: a thin facade over the domain apps' services.

No view here builds a queryset or contains a rule. Each resolves the request,
calls exactly one service, and serializes the result, so the story the API tells
is the story the domain modules already tell.
"""

from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import services as account_services
from apps.accounts.serializers import UserSerializer
from apps.analytics import services as analytics_services
from apps.analytics.models import MetricType
from apps.analytics.serializers import AnalyticSnapshotSerializer
from apps.common.permissions import IsJobOwner
from apps.ingestion import services as ingestion_services
from apps.ingestion.etl.errors import UnsafeFileError
from apps.ingestion.etl.pipeline import IngestionPolicy
from apps.ingestion.serializers import (
    IngestionJobSerializer,
    IngestionUploadSerializer,
)
from apps.instruments import services as instrument_services
from apps.instruments.filters import InstrumentFilter
from apps.instruments.serializers import (
    InstrumentDetailSerializer,
    InstrumentListSerializer,
)
from apps.marketdata import services as price_services
from apps.marketdata.serializers import PriceRecordSerializer

from . import services as api_services
from .serializers import InstrumentQualitySerializer


def parse_date_range(params):
    """Parses and validates ``start_date``/``end_date`` as one inclusive range.

    A malformed date or an inverted range is the client's mistake, so both come
    back as ``400`` under ``date_range`` rather than escaping as a ``500``. The
    parsing itself is the market-data service, so the facade and the history
    endpoint agree on what a date is.
    """
    try:
        start_date = price_services.parse_date(params.get("start_date"))
        end_date = price_services.parse_date(params.get("end_date"))
        price_services.validate_date_range(start_date, end_date)
    except ValueError as exc:
        raise ValidationError({"date_range": [str(exc)]}) from exc
    return start_date, end_date


class StockScopedMixin:
    """Resolves the ``{symbol}`` in the URL to one instrument, or ``404``s.

    Symbols are matched case-insensitively, because a symbol is an identifier
    that a client may have cased differently. Every stock sub-resource shares
    this one lookup so they cannot disagree about which instrument a URL names.
    """

    def get_stock(self):
        return get_object_or_404(
            instrument_services.get_instrument_queryset(),
            symbol__iexact=self.kwargs["symbol"],
        )


class MeView(APIView):
    """The authenticated caller's own profile.

    ``IsAuthenticated`` is listed explicitly because a view's
    ``permission_classes`` replaces the global default.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class MeAnalyticsView(APIView):
    """The caller's own activity summary.

    Deliberately the same service the ``/auth/usage/`` endpoint uses, so the two
    endpoints report identical numbers rather than two aggregations that have to
    be kept in step.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(account_services.get_user_usage_statistics(request.user))


class StockListAPIView(generics.ListAPIView):
    """Lists stocks, filterable and searchable like the instruments table.

    ``InstrumentListSerializer`` flattens the exchange to its code, so a page is
    one query and no nested objects. The service owns the ``select_related``.
    """

    serializer_class = InstrumentListSerializer
    filterset_class = InstrumentFilter
    search_fields = ["symbol", "name"]
    ordering_fields = ["symbol", "name", "asset_type", "created_at"]
    ordering = ["symbol"]

    def get_queryset(self):
        return instrument_services.get_instrument_queryset()


class StockDetailAPIView(StockScopedMixin, generics.RetrieveAPIView):
    """Retrieves one stock by symbol, case-insensitively, with its exchange nested."""

    serializer_class = InstrumentDetailSerializer

    def get_object(self):
        return self.get_stock()


class StockHistoryAPIView(StockScopedMixin, generics.ListAPIView):
    """Lists one stock's price history, optionally within a date range.

    Read-only: prices are produced by the ingestion pipeline. ``PriceRecordSerializer``
    renders only OHLC/volume/source, which is why the queryset is the join-free one
    from the service layer.
    """

    serializer_class = PriceRecordSerializer
    ordering_fields = ["trading_date", "close", "volume"]
    ordering = ["trading_date"]

    def get_queryset(self):
        start_date, end_date = parse_date_range(self.request.query_params)
        return price_services.get_prices_for_instrument(
            self.get_stock(), start_date=start_date, end_date=end_date
        )


class StockAnalyticsAPIView(StockScopedMixin, generics.ListAPIView):
    """Lists the analytical snapshots recorded for one stock.

    Reads stored metric rows -- the ones the pipeline wrote -- and is not the
    on-demand engine; that lives at ``/analytics/instruments/<id>/series/``.
    """

    serializer_class = AnalyticSnapshotSerializer
    ordering_fields = ["as_of_date", "metric_type", "value"]
    ordering = ["-as_of_date"]

    def get_queryset(self):
        params = self.request.query_params
        metric_type = params.get("metric_type") or None
        if metric_type and metric_type not in MetricType.values:
            raise ValidationError(
                {
                    "metric_type": [
                        (
                            f"'{metric_type}' is not a metric type; "
                            f"choose one of {list(MetricType.values)}."
                        )
                    ]
                }
            )

        start_date, end_date = parse_date_range(params)
        return analytics_services.list_snapshots_for_instrument(
            self.get_stock(),
            metric_type=metric_type,
            start_date=start_date,
            end_date=end_date,
        )


class StockQualityAPIView(StockScopedMixin, APIView):
    """Reviews one stock's price coverage over a range.

    Answers the question a data audit asks and the metrics cannot: the sessions
    the exchange was open that this instrument has no price for. An omitted range
    reviews everything on file.
    """

    def get(self, request, symbol):
        start_date, end_date = parse_date_range(request.query_params)
        quality = api_services.instrument_quality(
            self.get_stock(), start_date=start_date, end_date=end_date
        )
        return Response(InstrumentQualitySerializer(quality).data)


class JobETLAPIView(APIView):
    """Queues an ETL load and returns the job to poll.

    A load is long-running work, so it must not hold the request open. This
    stores the file, commits the job row, hands it to a worker, and answers
    ``202`` with the job id; the client polls ``/jobs/<id>/`` for status. A
    re-submission of the same bytes (or the same ``Idempotency-Key``) is a
    ``200`` replay of the original job rather than a second load.
    """

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        serializer = IngestionUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        policy = IngestionPolicy(
            on_invalid=data["on_invalid"],
            on_existing=data["on_existing"],
        )

        try:
            job, created = ingestion_services.submit_upload(
                user=request.user,
                uploaded_file=data["file"],
                source_type=data.get("source_type"),
                sheet_name=data.get("sheet_name"),
                policy=policy,
                idempotency_key=request.headers.get("Idempotency-Key") or None,
            )
        except (UnsafeFileError, ValueError) as exc:
            raise ValidationError({"file": [str(exc)]}) from exc

        return Response(
            {"job": IngestionJobSerializer(job).data, "created": created},
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )


class JobDetailAPIView(generics.RetrieveAPIView):
    """Retrieves one ETL job's status.

    Ownership is checked per-object; another user's job is a ``403`` (the row
    exists) and a missing one a ``404``. ``IsAuthenticated`` is listed
    explicitly because view-level permissions replace the global default.
    """

    serializer_class = IngestionJobSerializer
    permission_classes = [IsAuthenticated, IsJobOwner]

    def get_queryset(self):
        return ingestion_services.get_job_queryset()

    def get_object(self):
        job = get_object_or_404(self.get_queryset(), pk=self.kwargs["job_id"])
        self.check_object_permissions(self.request, job)
        return job


class JobQualityAPIView(APIView):
    """Returns the data-quality report for one ETL job.

    The report is a computed document assembled from the job, its rule summaries,
    and a sample of its failures, so it is returned as a plain JSON object rather
    than through a model serializer.
    """

    permission_classes = [IsAuthenticated, IsJobOwner]

    def get(self, request, job_id):
        job = get_object_or_404(ingestion_services.get_job_queryset(), pk=job_id)
        self.check_object_permissions(request, job)
        return Response(ingestion_services.report_for(job))
