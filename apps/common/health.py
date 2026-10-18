"""Liveness and readiness probes.

Two endpoints, and the difference between them is the whole point:

* ``/healthz`` (and its short alias ``/health``) -- **liveness**. "This process
  is running and can answer." It touches nothing: no database, no cache, no
  broker. A liveness probe wired to a dependency would restart a perfectly
  healthy web process every time MySQL blipped, turning a short database hiccup
  into an outage of the web tier.

* ``/readyz`` -- **readiness**. "This instance can serve real traffic now", and
  it names each dependency separately. A single red light that means "something
  is wrong" is the failure this avoids: "the database is down" and "the broker is
  down" have different remedies and different blast radii, and an operator should
  not have to guess which one they are looking at.

The web tier needs only the database to answer a read, so the *status code*
depends on the database alone. The broker is reported because the worker needs it
and a stuck queue is otherwise invisible; the cache is reported because a cache
outage is a slowdown, not an outage, and must not pull a healthy instance out of
a load balancer. This draws the same three lines as ``apps.ingestion.monitoring``,
for the same reason.

These are plain Django views, not DRF: a probe must never require a JWT, and it
should not pay for content negotiation it never uses.
"""

from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from apps.common import cache
from apps.ingestion import monitoring


@require_GET
def liveness(request):
    """Process liveness. No dependency is touched, so this fails only when the
    process is gone (no response at all) or wedged (the probe times out)."""
    return JsonResponse({"status": "ok"}, status=200)


@require_GET
def readiness(request):
    """Readiness, with each dependency reported separately.

    Contract
    --------
    out: ``{"status": ..., "dependencies": {database, broker, cache}}``, with a
         ``200`` when the web tier can serve and a ``503`` when it cannot.
    """
    database_up = monitoring.database_ok()
    broker_up = monitoring.broker_ok()
    cache_state = cache.health()  # True/False, or None when caching is switched off

    dependencies = {
        "database": "up" if database_up else "down",
        "broker": "up" if broker_up else "down",
        "cache": "off" if cache_state is None else ("up" if cache_state else "down"),
    }

    ready = database_up
    return JsonResponse(
        {"status": "ready" if ready else "not_ready", "dependencies": dependencies},
        status=200 if ready else 503,
    )
