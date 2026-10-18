"""URL routing for the reporting endpoints."""

from django.urls import path

from .views import AuditEventListView, PlatformAnalyticsView, UserAnalyticsView


app_name = "reports"

urlpatterns = [
    path("me/", UserAnalyticsView.as_view(), name="user-analytics"),
    path("platform/", PlatformAnalyticsView.as_view(), name="platform-analytics"),
    path("audit-events/", AuditEventListView.as_view(), name="audit-events"),
]
