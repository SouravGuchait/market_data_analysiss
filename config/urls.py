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

from apps.common import health as health_views


urlpatterns = [
    # Probes. Unauthenticated and dependency-aware by design; see
    # apps/common/health.py for why liveness and readiness are two endpoints and
    # not one. `/health` is kept as an alias because an external platform poller
    # already requests it (the SuppressProbeNotFound log filter exists for it).
    path("health", health_views.liveness, name="health-liveness"),
    path("healthz", health_views.liveness, name="healthz"),
    path("readyz", health_views.readiness, name="readyz"),
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
    # Docs are enabled by default only in development. Production deployments
    # can opt in explicitly with SERVE_API_DOCS=true.
    # ------------------------------------------------------------------ #
]

if settings.SERVE_API_DOCS:
    urlpatterns += [
        path("docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
        path("docs/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
        path("docs/schema/", SpectacularAPIView.as_view(), name="schema"),
    ]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
