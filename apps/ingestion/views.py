"""Ingestion job endpoints."""

from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import IsJobOwner

from . import services
from .serializers import (
    IngestionJobCreateSerializer,
    IngestionJobSerializer,
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

        return Response(IngestionJobSerializer(job).data, status=status.HTTP_201_CREATED)


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
