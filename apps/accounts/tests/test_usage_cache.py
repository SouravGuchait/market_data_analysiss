"""Caching of the usage summary.

The summary is the most expensive single read in the API -- it aggregates every
price row the caller owns -- and it is the one response that is genuinely
*private*, so it is also where a caching layer can do the most damage. These
tests cover both halves: that a hit is cheap and correct, and that one user can
never be served another's numbers.

Cached reads are off for the rest of the suite, so every test here opts in.
"""

from datetime import date
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from apps.accounts.tests.test_usage import USAGE_URL
from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.ingestion.models import IngestionJob
from apps.instruments.models import AssetType, Exchange, Instrument
from apps.marketdata.models import PriceRecord


class UsageCacheTestCase(AuthenticatedAPITestCase):
    """Two users, each with their own job and price rows."""

    def setUp(self):
        super().setUp()
        self.other = self.create_user("someone.else@example.com")
        self.exchange = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        self.instrument = Instrument.objects.create(
            symbol="AAPL",
            name="Apple Inc.",
            asset_type=AssetType.EQUITY,
            exchange=self.exchange,
            currency="USD",
        )
        self.job = IngestionJob.objects.create(
            created_by=self.user,
            source_type="csv",
            rows_received=100,
            rows_accepted=90,
            rows_rejected=10,
        )
        self.add_price(owner=self.user, job=self.job, day=date(2024, 3, 11))
        other_job = IngestionJob.objects.create(
            created_by=self.other, source_type="json", rows_received=50
        )
        self.add_price(owner=self.other, job=other_job, day=date(2024, 3, 12))

    def add_price(self, *, owner, job, day):
        return PriceRecord.objects.create(
            instrument=self.instrument,
            trading_date=day,
            open="100",
            high="105",
            low="95",
            close="100",
            volume=1000,
            source="test",
            ingestion_job=job,
        )


@override_settings(API_CACHE_ENABLED=True)
class UsageCacheTests(UsageCacheTestCase):
    def test_the_second_identical_request_is_answered_from_the_cache(self):
        first = self.get_as(self.user, USAGE_URL)
        second = self.get_as(self.user, USAGE_URL)

        self.assertEqual(first["X-Cache"], "MISS")
        self.assertEqual(second["X-Cache"], "HIT")

    def test_a_hit_avoids_the_aggregate_queries_entirely(self):
        with self.assertNumQueries(4):
            # The JWT user, then the three aggregates: job totals, jobs by
            # status, and the price totals.
            self.get_as(self.user, USAGE_URL)

        with self.assertNumQueries(1):
            # The JWT user only: the summary was already rendered.
            self.get_as(self.user, USAGE_URL)

    def test_a_cached_summary_is_identical_to_a_fresh_one(self):
        fresh = self.get_as(self.user, USAGE_URL)
        cached = self.get_as(self.user, USAGE_URL)

        self.assertEqual(cached.content, fresh.content)

    def test_one_users_summary_is_never_served_to_another(self):
        """The response is private, so the owner must be part of the key.

        Without the owner in the key the first caller to ask would populate an
        entry that every later caller would be handed -- the classic way a cache
        turns into a data leak.
        """
        mine = self.get_as(self.user, USAGE_URL)
        theirs = self.get_as(self.other, USAGE_URL)
        mine_again = self.get_as(self.user, USAGE_URL)

        self.assertEqual(mine.data["jobs"]["rows_received"], 100)
        self.assertEqual(theirs.data["jobs"]["rows_received"], 50)
        self.assertEqual(mine.data["prices"]["records_imported"], 1)
        self.assertEqual(theirs.data["prices"]["records_imported"], 1)

        # Neither user's answer came from the other's entry: each missed first,
        # then hit their own.
        self.assertEqual(theirs["X-Cache"], "MISS")
        self.assertEqual(mine_again["X-Cache"], "HIT")
        self.assertEqual(mine_again.data, mine.data)

    def test_an_anonymous_request_cannot_reach_the_cache(self):
        response = self.client.get(USAGE_URL)

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("X-Cache", response)


@override_settings(API_CACHE_ENABLED=True)
class UsageInvalidationTests(UsageCacheTestCase):
    def populated(self):
        response = self.get_as(self.user, USAGE_URL)
        self.assertEqual(response["X-Cache"], "MISS")
        return response

    def test_a_price_write_invalidates_the_summary(self):
        self.populated()

        with self.captureOnCommitCallbacks(execute=True):
            self.add_price(owner=self.user, job=self.job, day=date(2024, 3, 13))

        response = self.get_as(self.user, USAGE_URL)
        self.assertEqual(response["X-Cache"], "MISS")
        self.assertEqual(response.data["prices"]["records_imported"], 2)

    def test_a_bulk_price_write_invalidates_the_summary(self):
        """The pipeline writes with bulk_create, which sends no signals.

        So the pipeline says so itself -- see ``upsert_prices``. This exercises
        that call rather than the post_save receivers, because a cache that only
        notices row-at-a-time writes would miss every real ingestion.
        """
        from apps.ingestion.etl.persistence import SymbolIndex, upsert_prices

        self.populated()

        with self.captureOnCommitCallbacks(execute=True):
            upsert_prices(
                job=self.job,
                records=[
                    {
                        "symbol": "AAPL",
                        "trading_date": date(2024, 3, 14),
                        "open": "10",
                        "high": "11",
                        "low": "9",
                        "close": "10",
                        "volume": 5,
                        "source": "test",
                    }
                ],
                symbols=SymbolIndex({"AAPL": self.instrument.pk}),
            )

        response = self.get_as(self.user, USAGE_URL)
        self.assertEqual(response["X-Cache"], "MISS")
        self.assertEqual(response.data["prices"]["records_imported"], 2)

    def test_a_job_reaching_a_terminal_state_invalidates_the_summary(self):
        self.populated()

        with self.captureOnCommitCallbacks(execute=True):
            self.job.status = "completed"
            self.job.save(update_fields=["status"])

        response = self.get_as(self.user, USAGE_URL)
        self.assertEqual(response["X-Cache"], "MISS")
        self.assertEqual(response.data["jobs"]["by_status"], {"completed": 1})

    def test_a_progress_heartbeat_does_not_invalidate_the_summary(self):
        """A running job writes its row constantly and changes nothing that counts.

        Invalidating on each heartbeat would evict the entry every few seconds
        and make the cache pointless during exactly the load it exists to survive.
        """
        self.populated()

        with self.captureOnCommitCallbacks(execute=True):
            self.job.rows_processed = 10
            self.job.heartbeat_at = timezone.now()
            self.job.save(update_fields=["rows_processed", "heartbeat_at"])

        self.assertEqual(self.get_as(self.user, USAGE_URL)["X-Cache"], "HIT")

    def test_a_checkpoint_write_does_not_invalidate_the_summary(self):
        self.populated()

        with self.captureOnCommitCallbacks(execute=True):
            self.job.checkpoint = {"offset": 500, "total": 1000}
            self.job.rows_processed = 500
            self.job.save(update_fields=["checkpoint", "rows_processed"])

        self.assertEqual(self.get_as(self.user, USAGE_URL)["X-Cache"], "HIT")


class CacheIsDown:
    """A stand-in for Redis: every operation fails."""

    def __getattr__(self, name):
        def explode(*args, **kwargs):
            raise ConnectionError("redis is unreachable")

        return explode


class UsageCacheDegradationTests(UsageCacheTestCase):
    """The summary must survive its cache being unavailable."""

    @override_settings(API_CACHE_ENABLED=True)
    def test_the_summary_is_still_returned_when_the_cache_is_unreachable(self):
        with mock.patch("apps.common.cache.cache", CacheIsDown()):
            response = self.get_as(self.user, USAGE_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Cache"], "MISS")
        self.assertEqual(response.data["jobs"]["rows_received"], 100)
        self.assertEqual(response.data["prices"]["records_imported"], 1)

    @override_settings(API_CACHE_ENABLED=False)
    def test_caching_off_still_returns_the_summary(self):
        response = self.get_as(self.user, USAGE_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Cache"], "MISS")
        self.assertEqual(response.data["prices"]["records_imported"], 1)
