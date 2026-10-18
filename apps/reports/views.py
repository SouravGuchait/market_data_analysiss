"""Reporting endpoints.

Thin adapters, as everywhere else: parse the request (here there is nothing to
parse), call one service, render its dict. The payloads are computed documents
rather than model rows, so -- like the ingestion data-quality report -- they are
returned as plain objects with no ``ModelSerializer`` behind them.

The audit log is the exception: it *is* a list of model rows, so it is a
``ListAPIView`` over a queryset and is paginated and filtered like every other
list in the project.

Nothing here is cached. A report is read occasionally and is meant to reflect the
newest job immediately; the small, hot per-user summary at ``/auth/usage/`` is
the endpoint that pays for caching.
"""

from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAdministrator

from . import services
from .filters import AuditEventFilter
from .serializers import AuditEventSerializer


class UserAnalyticsView(APIView):
    """The authenticated user's own ingestion analytics.

    Self-scoped, and scoped *in the query*: the report is computed from
    ``IngestionJob.objects.filter(created_by=request.user)``, so another user's
    jobs are never selected, let alone aggregated. There is no path parameter and
    no ``user`` query parameter -- there is deliberately no way to ask this
    endpoint about anyone but yourself. Platform-wide figures are a separate,
    administrator-only endpoint.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        return Response(services.get_user_analytics(request.user))


class PlatformAnalyticsView(APIView):
    """Platform-wide ingestion analytics, for administrators only.

    ``IsAuthenticated`` is listed beside ``IsAdministrator`` on purpose: a view's
    ``permission_classes`` *replace* the global default, so without it an
    anonymous request would fail the role check and earn a ``403`` when the
    accurate answer is ``401``. With both, an anonymous caller is unauthenticated
    (``401``) and an authenticated analyst is forbidden (``403``).

    Unlike ``/me/``, this report is not scoped to the caller -- the permission is
    the scope. That is what makes it a platform report.
    """

    permission_classes = [IsAuthenticated, IsAdministrator]

    def get(self, request, *args, **kwargs):
        return Response(services.get_platform_analytics())


class AuditEventListView(generics.ListAPIView):
    """The paginated audit log, for administrators only.

    The one report that is not a computed document: it is a page of stored events.
    ``IsAuthenticated, IsAdministrator`` for the same 401-before-403 reason as the
    platform report, and paginated by the project-wide
    :class:`~apps.common.pagination.StandardResultsPagination`, so ``?page=`` and
    ``?page_size=`` work as they do everywhere else.

    The log is the compliance record the rest of the application writes into, so
    it is read-only and administrator-only: an analyst cannot see who did what,
    only their own reports at ``/me/``.
    """

    serializer_class = AuditEventSerializer
    permission_classes = [IsAuthenticated, IsAdministrator]
    filterset_class = AuditEventFilter
    ordering_fields = ["created_at", "action", "object_type"]
    ordering = ["-created_at"]
    search_fields = ["action", "object_type", "object_id", "request_id", "user__email"]

    def get_queryset(self):
        return services.list_audit_events()
