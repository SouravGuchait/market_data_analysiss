"""Operational endpoints that do not contain domain business logic."""

import logging

from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


logger = logging.getLogger("market_data.health")


@require_GET
def health_check(request):
    """Return service availability and verify the configured database is reachable."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        logger.exception("Health check failed because the database is unavailable")
        return JsonResponse({"status": "unavailable", "database": "unavailable"}, status=503)

    return JsonResponse({"status": "ok", "database": "ok"})
