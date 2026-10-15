"""URL routing for analytics."""

from django.urls import path

from .views import AnalyticSnapshotListAPIView


app_name = "analytics"

urlpatterns = [
    path(
        "instruments/<int:instrument_pk>/metrics/",
        AnalyticSnapshotListAPIView.as_view(),
        name="snapshot-list",
    ),
]
