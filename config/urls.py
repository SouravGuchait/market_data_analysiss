"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/instruments/", include("apps.instruments.urls")),
    path("api/v1/market-data/", include("apps.marketdata.urls")),
    path("api/v1/ingestion/", include("apps.ingestion.urls")),
    path("api/v1/analytics/", include("apps.analytics.urls")),
    # The public facade. Last so the domain namespaces above match first.
    path("api/v1/", include("apps.api.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
