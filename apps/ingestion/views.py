"""Ingestion job endpoints.

The read/create surface mirrors the PR's original design; the upload, report, and
rule-summary endpoints are the ETL upgrade: they are what actually run a load and
expose its data-quality verdict. Every view stays a thin adapter — the work is in
``services``.
"""

from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import services as audit
from apps.audit.events import AuditAction
from apps.common.permissions import IsJobOwner

from . import services
from .etl.errors import UnsafeFileError
from .etl.pipeline import IngestionPolicy
from .serializers import (
    IngestionJobCreateSerializer,
    IngestionJobSerializer,
    IngestionRuleSummarySerializer,
    IngestionUploadSerializer,
    IngestionValidationErrorSerializer,
)


class IngestionJobListCreateAPIView(generics.ListCreateAPIView):
    """Lists the caller's own jobs, and creates a new one.

    ``ListCreateAPIView`` rather than a full ``ModelViewSet`` because jobs are
    never updated or deleted through the API: list and create are the whole
    surface, so the class declares exactly those two actions.
    """

    ordering_fields = ["requested_at", "status", "rows_received"]
    ordering = ["-requested_at"]

    def get_queryset(self):
        # Ownership is enforced in the query itself, so another user's jobs are
        # never even selected, let alone serialized.
        params = self.request.query_params
        return services.list_jobs_for_user(
            self.request.user,
            status=params.get("status"),
            source_type=params.get("source_type"),
        )

    def get_serializer_class(self):
        if self.request.method == "POST":
            return IngestionJobCreateSerializer
        return IngestionJobSerializer

    def create(self, request, *args, **kwargs):
        # No try/except here: the serializer is a ModelSerializer over a field
        # with choices, so it rejects an unknown source_type itself and
        # is_valid(raise_exception=True) returns the 400. services.create_job
        # keeps its own guard for non-HTTP callers.
        serializer = IngestionJobCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        job = services.create_job(
            created_by=request.user,
            source_type=serializer.validated_data["source_type"],
        )

        # The creation is the auditable decision; the load it later triggers is a
        # separate operation. Recorded after the job exists, so the row names a
        # real object id.
        audit.record_event(
            action=AuditAction.JOB_CREATED,
            request=request,
            object_type="IngestionJob",
            object_id=job.pk,
            metadata={"source_type": job.source_type},
        )

        return Response(IngestionJobSerializer(job).data, status=status.HTTP_201_CREATED)


class IngestionJobUploadAPIView(APIView):
    """Uploads a source file and runs the synchronous ETL in one request.

    Separate from ``POST /`` on purpose: creating a job is a request to load, and
    this is the action that fulfils it. A client that only wants to record a job
    is not forced to send a file, and a client that has the file gets the load
    done in the same round trip.

    Returns ``201`` with both the resulting job and its data-quality report, so a
    caller needs one request to learn the outcome.
    """

    permission_classes = [IsAuthenticated]
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
            result = services.ingest_upload(
                user=request.user,
                uploaded_file=data["file"],
                source_type=data.get("source_type"),
                sheet_name=data.get("sheet_name"),
                policy=policy,
            )
        except (UnsafeFileError, ValueError) as exc:
            # The serializer checks the file first, so reaching here means the
            # source type could not be resolved. Either way it is the client's
            # input, not a server fault.
            raise ValidationError({"file": [str(exc)]}) from exc

        # A synchronous load is one auditable operation: the file arrived and was
        # loaded in the same request. The outcome and the policy decisions travel
        # in the metadata, so the log answers "what did this upload actually do"
        # without a second query.
        audit.record_event(
            action=AuditAction.UPLOAD_LOADED,
            request=request,
            object_type="IngestionJob",
            object_id=result.job.pk,
            metadata={
                "source_type": result.job.source_type,
                "status": result.job.status,
                "on_invalid": policy.on_invalid,
                "on_existing": policy.on_existing,
                "rows_received": result.job.rows_received,
                "rows_accepted": result.job.rows_accepted,
                "rows_rejected": result.job.rows_rejected,
            },
        )

        return Response(
            {
                "job": IngestionJobSerializer(result.job).data,
                "report": result.report,
            },
            status=status.HTTP_201_CREATED,
        )


class IngestionJobSubmitAPIView(APIView):
    """Accepts a file, records a job, and hands it to a worker.

    This is the asynchronous twin of ``upload/``. Where that endpoint blocks
    until the load is done and returns its report, this one stores the file,
    commits the job, enqueues it, and returns immediately with ``202 Accepted``.
    The client then polls ``GET /<id>/`` for status, or ``/<id>/report/`` for the
    data-quality verdict once it is terminal.

    ``created: false`` with ``200 OK`` means the submission was a replay: a
    client that already sent these exact bytes (or the same ``Idempotency-Key``)
    gets the original job back rather than a second load. That is what makes a
    retry after a timeout safe.
    """

    permission_classes = [IsAuthenticated]
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
            job, created = services.submit_upload(
                user=request.user,
                uploaded_file=data["file"],
                source_type=data.get("source_type"),
                sheet_name=data.get("sheet_name"),
                policy=policy,
                # A client-supplied key names the *operation*, so a retry maps to
                # the same job even if the bytes were re-encoded in transit.
                idempotency_key=request.headers.get("Idempotency-Key") or None,
            )
        except (UnsafeFileError, ValueError) as exc:
            raise ValidationError({"file": [str(exc)]}) from exc

        # Only the call that *created* the job records an event. A replay is not a
        # new decision -- nothing was queued and nothing changed -- and logging it
        # would let a retry loop fill the trail with duplicates of one operation.
        if created:
            audit.record_event(
                action=AuditAction.JOB_QUEUED,
                request=request,
                object_type="IngestionJob",
                object_id=job.pk,
                metadata={
                    "source_type": job.source_type,
                    "on_invalid": policy.on_invalid,
                    "on_existing": policy.on_existing,
                },
            )

        return Response(
            {"job": IngestionJobSerializer(job).data, "created": created},
            status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
        )


class IngestionJobCancelAPIView(APIView):
    """Requests cancellation of a job that has not finished.

    Cancellation is cooperative. A pending job is simply marked terminal and is
    never claimed. A running job is marked terminal too, and the worker notices
    at its next chunk boundary — so the load stops quickly, but between chunks,
    not in the middle of a write. Cancelling an already-terminal job is a no-op
    and returns the job unchanged, which makes the endpoint safe to retry.
    """

    permission_classes = [IsAuthenticated, IsJobOwner]

    def post(self, request, pk):
        job = get_object_or_404(services.get_job_queryset(), pk=pk)
        self.check_object_permissions(request, job)

        # Cancelling an already-terminal job is a no-op and is not recorded: the
        # log records decisions, and "do nothing, it is already cancelled" is not
        # one. The status before the call is what makes that distinction.
        was_active = not job.is_terminal
        job = services.cancel_job(job)
        if was_active:
            audit.record_event(
                action=AuditAction.JOB_CANCELLED,
                request=request,
                object_type="IngestionJob",
                object_id=job.pk,
                metadata={"source_type": job.source_type},
            )

        return Response(IngestionJobSerializer(job).data)


class IngestionJobRetrieveAPIView(generics.RetrieveAPIView):
    """Retrieves the status of one job.

    Ownership is checked per-object. ``IsAuthenticated`` is listed explicitly
    because a view's ``permission_classes`` replaces the global default, and
    without it an anonymous request would get a 403 instead of the more accurate
    401.
    """

    serializer_class = IngestionJobSerializer
    permission_classes = [IsAuthenticated, IsJobOwner]

    def get_queryset(self):
        return services.get_job_queryset()

    def get_object(self):
        job = get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])
        self.check_object_permissions(self.request, job)
        return job


class IngestionJobReportAPIView(APIView):
    """Returns the data-quality report for one job.

    The report is a computed document assembled from the job, its rule summaries,
    and a sample of its failures — not a model — so it is returned as a plain
    JSON object rather than through a ``ModelSerializer``.
    """

    permission_classes = [IsAuthenticated, IsJobOwner]

    def get(self, request, pk):
        job = get_object_or_404(services.get_job_queryset(), pk=pk)
        self.check_object_permissions(request, job)
        return Response(services.report_for(job))


class IngestionJobValidationErrorListAPIView(generics.ListAPIView):
    """Lists the validation errors recorded against one job."""

    serializer_class = IngestionValidationErrorSerializer
    ordering_fields = ["row_number", "error_code"]
    ordering = ["row_number"]
    permission_classes = [IsAuthenticated, IsJobOwner]

    def get_queryset(self):
        job = get_object_or_404(services.get_job_queryset(), pk=self.kwargs["job_pk"])
        self.check_object_permissions(self.request, job)
        return services.get_validation_errors_for_job(
            job, error_code=self.request.query_params.get("error_code")
        )


class IngestionJobRuleSummaryListAPIView(generics.ListAPIView):
    """Lists the rule-level PASS/FAIL verdicts for one job.

    Unpaginated: the pipeline evaluates a fixed set of rules, so a job has at most
    one row per rule. A page wrapper around a dozen rows would add noise, the same
    reasoning as the exchanges endpoint.
    """

    serializer_class = IngestionRuleSummarySerializer
    ordering_fields = ["rule_name", "passed", "failed"]
    ordering = ["rule_name"]
    pagination_class = None
    permission_classes = [IsAuthenticated, IsJobOwner]

    def get_queryset(self):
        job = get_object_or_404(services.get_job_queryset(), pk=self.kwargs["job_pk"])
        self.check_object_permissions(self.request, job)
        return services.get_rule_summaries_for_job(job)
