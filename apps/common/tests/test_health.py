"""The liveness and readiness probes.

These assert the property that makes the two endpoints worth having: liveness
must not care about dependencies, and readiness must name the one that is down
rather than reporting a single undifferentiated "unhealthy".
"""

from unittest import mock

from django.test import TestCase
from django.urls import reverse


class LivenessTests(TestCase):
    def test_liveness_is_ok_even_when_every_dependency_is_down(self):
        """Liveness answers "the process is up", so a dead database must not
        change its answer -- otherwise a database blip restarts the web tier."""
        with (
            mock.patch("apps.common.health.monitoring.database_ok", return_value=False),
            mock.patch("apps.common.health.monitoring.broker_ok", return_value=False),
        ):
            response = self.client.get(reverse("healthz"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_the_short_health_alias_answers_too(self):
        response = self.client.get(reverse("health-liveness"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class ReadinessTests(TestCase):
    def _readiness(self, *, database: bool, broker: bool, cache_state):
        with (
            mock.patch("apps.common.health.monitoring.database_ok", return_value=database),
            mock.patch("apps.common.health.monitoring.broker_ok", return_value=broker),
            mock.patch("apps.common.health.cache.health", return_value=cache_state),
        ):
            return self.client.get(reverse("readyz"))

    def test_ready_when_the_database_answers(self):
        response = self._readiness(database=True, broker=True, cache_state=True)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(
            body["dependencies"],
            {"database": "up", "broker": "up", "cache": "up"},
        )

    def test_not_ready_when_the_database_is_down(self):
        response = self._readiness(database=False, broker=True, cache_state=True)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")

    def test_each_dependency_is_named_separately(self):
        """A down broker with a live database is still ready for reads, and the
        body says *which* dependency is missing."""
        response = self._readiness(database=True, broker=False, cache_state=False)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["dependencies"],
            {"database": "up", "broker": "down", "cache": "down"},
        )

    def test_a_disabled_cache_is_reported_as_off_not_down(self):
        """Caching switched off (LocMemCache disabled, or API_CACHE_ENABLED)
        must not read as a failure."""
        response = self._readiness(database=True, broker=True, cache_state=None)

        self.assertEqual(response.json()["dependencies"]["cache"], "off")

    def test_a_dead_cache_does_not_make_the_instance_unready(self):
        response = self._readiness(database=True, broker=True, cache_state=False)

        self.assertEqual(response.status_code, 200)
