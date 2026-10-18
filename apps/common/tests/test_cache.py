"""Tests for the read-through cache layer.

These exercise :mod:`apps.common.cache` directly. The cache is switched *off* for
the suite by default (see ``API_CACHE_ENABLED`` in settings), so every test here
opts in explicitly -- which is also what makes the behaviour under test obvious
from the test itself.
"""

import threading
import time
from unittest import mock

from django.core.cache import cache as django_cache
from django.test import TestCase, override_settings

from apps.common import cache


class DownBackend:
    """Stands in for a Redis that cannot be reached.

    Any attribute access returns a callable that raises, which is the shape a
    dead cache presents through the Django cache API: every operation fails, and
    none of them fail in a way the caller can plan for.
    """

    def __getattr__(self, name):
        def explode(*args, **kwargs):
            raise ConnectionError("the cache is unreachable")

        return explode


class UnincrementableBackend:
    """A cache whose counter cannot be incremented.

    This is what Redis does when the stored counter is outside its signed 64-bit
    integer range -- or is the wrong type: ``INCR`` raises instead of advancing.
    LocMemCache cannot present this, because it increments a Python int, whose
    size is unbounded; the point of the stand-in is to reach the failure on a
    test host that has no Redis.
    """

    class ResponseError(Exception):
        pass

    def __init__(self):
        self.store: dict = {}

    def incr(self, key, *args, **kwargs):
        raise self.ResponseError("value is not an integer or out of range")

    def set(self, key, value, timeout=None):
        self.store[key] = value

    def get(self, key, default=None):
        return self.store.get(key, default)

    def delete(self, key):
        self.store.pop(key, None)


@override_settings(API_CACHE_ENABLED=True)
class CacheTestCase(TestCase):
    """Base case with a cache that is on, empty, and version-free."""

    def setUp(self):
        super().setUp()
        django_cache.clear()


class ReadThroughTests(CacheTestCase):
    def test_a_miss_computes_and_a_hit_does_not(self):
        calls = []

        def producer():
            calls.append(1)
            return {"value": 42}

        first = cache.cached("unit", parts={"a": 1}, ttl=60, producer=producer)
        second = cache.cached("unit", parts={"a": 1}, ttl=60, producer=producer)

        self.assertEqual(first, {"value": 42})
        self.assertEqual(second, {"value": 42})
        self.assertEqual(len(calls), 1, "the second read should have been a hit")

    def test_different_dimensions_do_not_share_an_entry(self):
        one = cache.cached("unit", parts={"instrument": 1}, ttl=60, producer=lambda: "one")
        two = cache.cached("unit", parts={"instrument": 2}, ttl=60, producer=lambda: "two")

        self.assertEqual((one, two), ("one", "two"))

    def test_a_scope_separates_entries_that_are_otherwise_identical(self):
        mine = cache.cached(
            "unit", parts={"a": 1}, scope="user:1", ttl=60, producer=lambda: "mine"
        )
        theirs = cache.cached(
            "unit", parts={"a": 1}, scope="user:2", ttl=60, producer=lambda: "theirs"
        )

        self.assertEqual((mine, theirs), ("mine", "theirs"))

    def test_a_stored_value_survives_a_json_round_trip_unchanged(self):
        payload = {
            "count": 2,
            "next": None,
            "previous": None,
            "results": [
                {"symbol": "AAPL", "value": "0.08000000", "as_of_date": "2024-03-14"},
                {"symbol": "MSFT", "value": "-0.01400000", "as_of_date": "2024-03-13"},
            ],
        }

        cache.remember(cache.build_key("unit", parts={}), payload, 60)
        self.assertEqual(cache.cached("unit", parts={}, ttl=60, producer=None), payload)


class InvalidationTests(CacheTestCase):
    def test_invalidating_a_namespace_orphans_every_entry_in_it(self):
        cache.cached("unit", parts={"a": 1}, ttl=60, producer=lambda: "first")

        cache.invalidate("unit")

        calls = []

        def producer():
            calls.append(1)
            return "second"

        self.assertEqual(
            cache.cached("unit", parts={"a": 1}, ttl=60, producer=producer), "second"
        )
        self.assertEqual(len(calls), 1)

    def test_invalidating_one_namespace_leaves_another_alone(self):
        cache.cached("one", parts={}, ttl=60, producer=lambda: "one")
        cache.cached("two", parts={}, ttl=60, producer=lambda: "two")

        cache.invalidate("one")

        def explode():
            raise AssertionError("the other namespace should still be cached")

        self.assertEqual(cache.cached("two", parts={}, ttl=60, producer=explode), "two")

    def test_the_version_is_part_of_the_key(self):
        before = cache.build_key("unit", parts={})
        cache.invalidate("unit")
        after = cache.build_key("unit", parts={})

        self.assertEqual(before, "unit:v1")
        self.assertNotEqual(after, before)

    def test_invalidating_twice_moves_the_version_forward_twice(self):
        cache.invalidate("unit")
        first = cache.get_version("unit")
        cache.invalidate("unit")

        self.assertGreater(cache.get_version("unit"), first)

    def test_the_fallback_version_stays_within_redis_integer_range(self):
        """The counter is advanced with Redis ``INCR``, a signed 64-bit
        operation. A fallback version at or above ``2**63`` would be stored but
        could never be incremented, silently freezing the namespace -- so the
        fallback must fit. Regression guard: the original used a 122-bit
        ``uuid4().int``, which LocMemCache happily increments and Redis cannot."""
        version = cache._version_after_a_lost_counter("unit")

        self.assertGreater(version, 0)
        self.assertLess(version, 2**63)

    def test_a_counter_redis_cannot_increment_is_reseeded_not_frozen(self):
        """An unincrementable counter (an out-of-range value left by an older
        build, or a wrong type) must be replaced rather than blocking every
        future invalidation of the namespace."""
        backend = UnincrementableBackend()

        with mock.patch("apps.common.cache.cache", backend):
            cache.invalidate("unit")
            version = cache.get_version("unit")

        self.assertGreater(version, 0)
        self.assertLess(version, 2**63)

    def test_a_lost_version_counter_cannot_resurrect_orphaned_entries(self):
        """Eviction of the counter must not restart the count and re-adopt entries.

        Simulated the way it would actually happen: entries are live under one
        version, the counter is evicted, and something invalidates again.
        """
        cache.invalidate("unit")
        stale_key = cache.build_key("unit", parts={"a": 1})
        cache.remember(stale_key, "stale", 300)
        version_with_stale_entry = cache.get_version("unit")

        django_cache.delete("md:ver:unit")  # evicted under memory pressure
        cache.invalidate("unit")

        # The stale entry is still physically in the cache; what matters is that
        # the same question no longer produces its key.
        self.assertNotEqual(cache.get_version("unit"), version_with_stale_entry)
        self.assertNotEqual(cache.build_key("unit", parts={"a": 1}), stale_key)


class KeyTests(CacheTestCase):
    def test_a_key_reads_like_the_question_it_answers(self):
        key = cache.build_key(
            "analytics",
            parts={"instrument": 13, "from": "2024-01-01", "page": 2},
            scope="role:analyst",
        )

        self.assertEqual(key, "analytics:v1:role:analyst:from=2024-01-01:instrument=13:page=2")

    def test_absent_and_empty_values_produce_the_same_key(self):
        self.assertEqual(
            cache.build_key("unit", parts={"metric": None, "page": ""}),
            cache.build_key("unit", parts={}),
        )

    def test_an_oversized_key_becomes_a_digest_of_itself(self):
        key = cache.build_key("unit", parts={f"p{i}": "x" * 20 for i in range(40)})

        self.assertLessEqual(len(key), cache.MAX_KEY_LENGTH)
        # Still a distinct key: the digest is of the whole readable string.
        other = cache.build_key("unit", parts={f"p{i}": "y" * 20 for i in range(40)})
        self.assertNotEqual(key, other)


class ConcurrencyTests(CacheTestCase):
    @override_settings(API_CACHE_STAMPEDE_WAIT_SECONDS=2.0, API_CACHE_LOCK_SECONDS=5)
    def test_concurrent_misses_on_a_cold_key_compute_once(self):
        """Eight simultaneous first requests must cost one computation, not eight."""
        threads = 8
        barrier = threading.Barrier(threads)
        calls = []
        results = []
        guard = threading.Lock()

        def producer():
            with guard:
                calls.append(1)
            # Slow enough that every other thread is certain to arrive while
            # this one is still working.
            time.sleep(0.05)
            return "computed once"

        def worker():
            barrier.wait()
            value = cache.cached("cold", parts={"k": 1}, ttl=60, producer=producer)
            with guard:
                results.append(value)

        workers = [threading.Thread(target=worker) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)

        self.assertEqual(results, ["computed once"] * threads)
        self.assertEqual(len(calls), 1)

    @override_settings(API_CACHE_STAMPEDE_WAIT_SECONDS=0.05)
    def test_a_lock_held_by_another_request_never_blocks_a_response(self):
        """The lock is an optimisation. A held one costs a query, never a hang."""
        key = cache.build_key("held", parts={"k": 1})
        self.assertTrue(cache._add(f"{key}:lock", "another-request", 30))

        calls = []

        def producer():
            calls.append(1)
            return "computed anyway"

        started = time.monotonic()
        value = cache.cached("held", parts={"k": 1}, ttl=60, producer=producer)
        elapsed = time.monotonic() - started

        self.assertEqual(value, "computed anyway")
        self.assertEqual(len(calls), 1)
        self.assertLess(elapsed, 1.0, "the request waited for a lock that was not coming")


class DegradationTests(CacheTestCase):
    """A cache that is down is a cache that misses -- never an error, never a wait."""

    def test_a_lookup_against_a_dead_backend_is_a_miss(self):
        with mock.patch.object(cache, "cache", DownBackend()):
            self.assertIsNone(cache.lookup("anything"))

    @override_settings(API_CACHE_STAMPEDE_WAIT_SECONDS=30)
    def test_a_read_against_a_dead_backend_is_answered_by_the_producer(self):
        with mock.patch.object(cache, "cache", DownBackend()):
            started = time.monotonic()
            value = cache.cached(
                "unit", parts={}, ttl=60, producer=lambda: "from the database"
            )
            elapsed = time.monotonic() - started

        self.assertEqual(value, "from the database")
        # The stampede wait is 30s here on purpose: an unreachable cache must not
        # be waited out, because nothing is coming.
        self.assertLess(elapsed, 1.0)

    def test_health_is_false_against_a_dead_backend(self):
        with mock.patch.object(cache, "cache", DownBackend()):
            self.assertFalse(cache.health())

    def test_health_is_true_against_a_live_backend(self):
        self.assertTrue(cache.health())

    @override_settings(API_CACHE_ENABLED=False)
    def test_caching_can_be_switched_off_without_touching_the_callers(self):
        calls = []

        def producer():
            calls.append(1)
            return "fresh every time"

        self.assertEqual(cache.cached("unit", parts={}, ttl=60, producer=producer), "fresh every time")
        self.assertEqual(cache.cached("unit", parts={}, ttl=60, producer=producer), "fresh every time")
        self.assertEqual(len(calls), 2, "caching is off, so both reads compute")
        self.assertIsNone(cache.health())
