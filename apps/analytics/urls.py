"""URL routing for analytics."""

from django.urls import path

from .views import (
    AnalyticSnapshotListAPIView,
    MetricSeriesAPIView,
    RankingAPIView,
)


app_name = "analytics"

urlpatterns = [
    # Stored snapshots: what the pipeline wrote.
    path(
        "instruments/<int:instrument_pk>/metrics/",
        AnalyticSnapshotListAPIView.as_view(),
        name="snapshot-list",
    ),
    # The engine computed on demand: one metric of one instrument.
    path(
        "instruments/<int:instrument_pk>/series/",
        MetricSeriesAPIView.as_view(),
        name="metric-series",
    ),
    # The cross-sectional metric: symbols compared, no instrument in the path.
    path(
        "rankings/",
        RankingAPIView.as_view(),
        name="ranking",
    ),
]
