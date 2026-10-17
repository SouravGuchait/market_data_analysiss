"""URL routing for ingestion jobs."""

from django.urls import path

from .views import (
    IngestionJobCancelAPIView,
    IngestionJobListCreateAPIView,
    IngestionJobReportAPIView,
    IngestionJobRetrieveAPIView,
    IngestionJobRuleSummaryListAPIView,
    IngestionJobSubmitAPIView,
    IngestionJobUploadAPIView,
    IngestionJobValidationErrorListAPIView,
)


app_name = "ingestion"

urlpatterns = [
    path("", IngestionJobListCreateAPIView.as_view(), name="list-create"),
    path("upload/", IngestionJobUploadAPIView.as_view(), name="upload"),
    path("submit/", IngestionJobSubmitAPIView.as_view(), name="submit"),
    path("<uuid:pk>/", IngestionJobRetrieveAPIView.as_view(), name="detail"),
    path("<uuid:pk>/cancel/", IngestionJobCancelAPIView.as_view(), name="cancel"),
    path("<uuid:pk>/report/", IngestionJobReportAPIView.as_view(), name="report"),
    path(
        "<uuid:job_pk>/validation-errors/",
        IngestionJobValidationErrorListAPIView.as_view(),
        name="validation-errors",
    ),
    path(
        "<uuid:job_pk>/rule-summaries/",
        IngestionJobRuleSummaryListAPIView.as_view(),
        name="rule-summaries",
    ),
]
