"""URL routing for ingestion jobs."""

from django.urls import path

from .views import (
    IngestionJobListCreateAPIView,
    IngestionJobRetrieveAPIView,
    IngestionJobValidationErrorListAPIView,
)


app_name = "ingestion"

urlpatterns = [
    path("", IngestionJobListCreateAPIView.as_view(), name="list-create"),
    path("<uuid:pk>/", IngestionJobRetrieveAPIView.as_view(), name="detail"),
    path(
        "<uuid:job_pk>/validation-errors/",
        IngestionJobValidationErrorListAPIView.as_view(),
        name="validation-errors",
    ),
]
