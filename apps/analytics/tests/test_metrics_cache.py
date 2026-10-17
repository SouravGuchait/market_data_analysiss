"""Caching of the metrics endpoint.

Everything here opts in with ``API_CACHE_ENABLED=True``: cached reads are off for
the rest of the suite, so what each test exercises is stated by the test.
"""

from datetime import date
from unittest import mock

from django.test import override_settings
from rest_framework.test import APIRequestFactory

from apps.analytics.models import MetricType

from .test_metrics_api import MetricsAPITestCase


@override_settings(API_CACHE_ENABLED=True)
class MetricsCacheTests(MetricsAPITestCase):
    def setUp(self):
        super().setUp()  # the shared mixin empties the cache for this test
        self.add_snapshot(day=date(2024, 3, 14), value="0.5")
        self.add_snapshot(day=date(2024, 3, 13), metric=MetricType.VOLATILITY, value="0.2")

    def test_the_second_identical_request_is_answered_from_the_cache(self):
        first = self.get_as(self.user, self.url)
        second = self.get_as(self.user, self.url)

        self.assertEqual(first["X-Cache"], "MISS")
        self.assertEqual(second["X-Cache"], "HIT")

    def test_a_hit_costs_no_queries_and_a_miss_costs_the_page(self):
        with self.assertNumQueries(4):
            # The JWT user, the instrument, the paginator's COUNT, the page.
            self.get_as(self.user, self.url)

        with self.assertNumQueries(1):
            # Just the JWT user: the body was already rendered.
            self.get_as(self.user, self.url)

    def test_a_cached_body_is_identical_to_a_fresh_one(self):
        """A hit must be indistinguishable from a miss, byte for byte.

        Decimals, dates, and the ``next``/``previous`` URLs are all produced by
        DRF for the fresh body and by a JSON round trip for the cached one, which
        is exactly where a caching layer usually starts lying.
        """
        fresh = self.get_as(self.user, self.url)
        cached = self.get_as(self.user, self.url)

        self.assertEqual(cached.content, fresh.content)

    def test_a_different_metric_is_a_different_entry(self):
        self.get_as(self.user, self.url)

        response = self.get_as(self.user, f"{self.url}?metric_type=volatility")

        self.assertEqual(response["X-Cache"], "MISS")
        self.assertEqual(response.data["count"], 1)

    def test_a_different_lookback_is_a_different_entry(self):
        self.get_as(self.user, f"{self.url}?lookback_days=30")

        response = self.get_as(self.user, f"{self.url}?lookback_days=90")

        self.assertEqual(response["X-Cache"], "MISS")

    def test_a_different_date_range_is_a_different_entry(self):
        self.get_as(self.user, self.url)

        response = self.get_as(self.user, f"{self.url}?start_date=2024-03-14")

        self.assertEqual(response["X-Cache"], "MISS")

    def test_a_different_page_is_a_different_entry(self):
        self.get_as(self.user, f"{self.url}?page_size=1&page=1")

        response = self.get_as(self.user, f"{self.url}?page_size=1&page=2")

        self.assertEqual(response["X-Cache"], "MISS")

    def test_the_requesting_role_is_part_of_the_key(self):
        """Scope is recorded even while every role sees the same rows.

        Snapshots are reference data, so the analyst's answer is correct for the
        administrator too -- but the key says which role asked, so that narrowing
        the queryset by role later cannot silently hand one role another's page.
        """
        analyst = self.get_as(self.user, self.url)
        self.assertEqual(analyst["X-Cache"], "MISS")

        administrator = self.create_administrator()
        response = self.get_as(administrator, self.url)

        self.assertEqual(response["X-Cache"], "MISS")

    def test_writing_a_snapshot_invalidates_the_cached_page(self):
        self.get_as(self.user, self.url)
        self.assertEqual(self.get_as(self.user, self.url)["X-Cache"], "HIT")

        with self.captureOnCommitCallbacks(execute=True):
            self.add_snapshot(day=date(2024, 3, 15), value="0.7")

        response = self.get_as(self.user, self.url)
        self.assertEqual(response["X-Cache"], "MISS")
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(response.data["results"][0]["value"], "0.70000000")

    def test_deleting_a_snapshot_invalidates_the_cached_page(self):
        self.get_as(self.user, self.url)

        with self.captureOnCommitCallbacks(execute=True):
            self.instrument.snapshots.first().delete()

        response = self.get_as(self.user, self.url)
        self.assertEqual(response["X-Cache"], "MISS")
        self.assertEqual(response.data["count"], 1)

    def test_the_cache_key_records_the_request_that_produced_it(self):
        response = self.get_as(self.user, f"{self.url}?lookback_days=90")

        # The readable key is the point: a key you can read in redis-cli is a key
        # you can reason about before deleting it.
        self.assertEqual(response["X-Cache"], "MISS")
        key = self.endpoint_key(f"{self.url}?lookback_days=90")
        self.assertIn(f"instrument={self.instrument.pk}", key)
        self.assertIn("lookback_days=90", key)
        self.assertIn("role:analyst", key)
        self.assertIn("v1", key)

    def endpoint_key(self, url):
        """Asks the real view what key it would have used.

        Derived from the view rather than from a literal, so this test documents
        the key instead of keeping a second copy of it that can drift.
        """
        from apps.analytics.views import AnalyticSnapshotListAPIView

        view = AnalyticSnapshotListAPIView()
        view.kwargs = {"instrument_pk": self.instrument.pk}
        # A DRF Request (so ``query_params`` exists), authenticated as the caller.
        request = view.initialize_request(APIRequestFactory().get(url))
        request.user = self.user
        view.request = request
        return view.get_cache_key()


class MetricsCacheDegradationTests(MetricsAPITestCase):
    """A Redis outage must cost latency, not correctness or a 500."""

    def setUp(self):
        super().setUp()
        self.add_snapshot(day=date(2024, 3, 14), value="0.5")

    @override_settings(API_CACHE_ENABLED=True)
    def test_the_endpoint_still_answers_when_the_cache_is_unreachable(self):
        with mock.patch("apps.common.cache.cache", CacheIsDown()):
            first = self.get_as(self.user, self.url)
            second = self.get_as(self.user, self.url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["count"], 1)
        self.assertEqual(first.content, second.content)
        # Nothing could be stored, so nothing can be a hit.
        self.assertEqual([first["X-Cache"], second["X-Cache"]], ["MISS", "MISS"])

    @override_settings(API_CACHE_ENABLED=True, API_CACHE_STAMPEDE_WAIT_SECONDS=30)
    def test_an_unreachable_cache_is_not_waited_out(self):
        """With no lock to take there is nothing to wait for, so do not wait."""
        import time

        with mock.patch("apps.common.cache.cache", CacheIsDown()):
            started = time.monotonic()
            response = self.get_as(self.user, self.url)
            elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 200)
        # The stampede wait is 30 seconds here: if the degraded path waited it
        # out, this test would take half a minute instead of failing fast.
        self.assertLess(elapsed, 5.0)

    @override_settings(API_CACHE_ENABLED=False)
    def test_caching_off_is_indistinguishable_from_a_miss(self):
        response = self.get_as(self.user, self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Cache"], "MISS")


class CacheIsDown:
    """A stand-in for Redis: every operation fails."""

    def __getattr__(self, name):
        def explode(*args, **kwargs):
            raise ConnectionError("redis is unreachable")

        return explode
