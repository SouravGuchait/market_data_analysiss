"""URL routing for the public API facade.

Mounted once at ``/api/v1/`` beside the domain namespaces, because this surface
spans them: ``stocks/`` reads instruments and prices, ``jobs/`` reads ingestion,
``me/`` reads accounts.
"""

from django.urls import path

from .views import (
    JobDetailAPIView,
    JobETLAPIView,
    JobQualityAPIView,
    MeAnalyticsView,
    MeView,
    StockAnalyticsAPIView,
    StockDetailAPIView,
    StockHistoryAPIView,
    StockListAPIView,
    StockQualityAPIView,
)

app_name = "api"

urlpatterns = [
    path("me/", MeView.as_view(), name="me"),
    path("me/analytics/", MeAnalyticsView.as_view(), name="me-analytics"),
    path("stocks/", StockListAPIView.as_view(), name="stock-list"),
    path("stocks/<str:symbol>/", StockDetailAPIView.as_view(), name="stock-detail"),
    path(
        "stocks/<str:symbol>/history/",
        StockHistoryAPIView.as_view(),
        name="stock-history",
    ),
    path(
        "stocks/<str:symbol>/analytics/",
        StockAnalyticsAPIView.as_view(),
        name="stock-analytics",
    ),
    path(
        "stocks/<str:symbol>/quality/",
        StockQualityAPIView.as_view(),
        name="stock-quality",
    ),
    path("jobs/etl/", JobETLAPIView.as_view(), name="job-etl"),
    path("jobs/<uuid:job_id>/", JobDetailAPIView.as_view(), name="job-detail"),
    path(
        "jobs/<uuid:job_id>/quality/",
        JobQualityAPIView.as_view(),
        name="job-quality",
    ),
]
