"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/instruments/", include("apps.instruments.urls")),
    path("api/v1/market-data/", include("apps.marketdata.urls")),
    path("api/v1/ingestion/", include("apps.ingestion.urls")),
    path("api/v1/analytics/", include("apps.analytics.urls")),
    path("api/v1/reports/", include("apps.reports.urls")),
    # The public facade. Last so the domain namespaces above match first.
    path("api/v1/", include("apps.api.urls")),
    # ------------------------------------------------------------------ #
    # Interactive API documentation.
    #
    # /docs/        Swagger UI -- the browsable, try-it-out reader.
    # /docs/redoc/  ReDoc -- the same schema as continuous prose.
    # /docs/schema/ the raw OpenAPI 3 document both readers consume.
    #
    # These are served unconditionally so the docs are available wherever the
    # app runs. To keep the schema out of production, wrap these three paths in
    # ``if settings.DEBUG:`` rather than deleting them.
    # ------------------------------------------------------------------ #
    path("docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("docs/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    path("docs/schema/", SpectacularAPIView.as_view(), name="schema"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
