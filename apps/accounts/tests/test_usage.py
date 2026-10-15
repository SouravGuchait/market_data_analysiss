"""Tests for the user usage endpoint."""

from datetime import date

from django.test import TestCase

from apps.accounts import services
from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.ingestion.models import IngestionJob, IngestionValidationError
from apps.instruments.models import AssetType, Exchange, Instrument
from apps.marketdata.models import PriceRecord

USAGE_URL = "/api/v1/auth/usage/"


class UsageServiceTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            email="user@example.com", password="drf-portfolio-42!"
        )
        job = IngestionJob.objects.create(created_by=self.user, source_type="csv")
        job.rows_received, job.rows_accepted, job.rows_rejected = 100, 90, 10
        job.save()
        IngestionValidationError.objects.create(
            job=job, row_number=1, error_code="not_a_number"
        )

    def test_the_statistics_aggregate_in_sql(self):
        stats = services.get_user_usage_statistics(self.user)

        self.assertEqual(stats["jobs"]["total"], 1)
        self.assertEqual(stats["jobs"]["rows_received"], 100)
        self.assertEqual(stats["jobs"]["by_status"], {"pending": 1})

    def test_a_user_with_no_activity_gets_zeroes_not_errors(self):
        from django.contrib.auth import get_user_model

        fresh = get_user_model().objects.create_user(
            email="fresh@example.com", password="drf-portfolio-42!"
        )
        stats = services.get_user_usage_statistics(fresh)

        self.assertEqual(stats["jobs"]["total"], 0)
        self.assertEqual(stats["jobs"]["by_status"], {})
        self.assertEqual(stats["prices"]["records_imported"], 0)


class UserUsageAPITests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_user("someone.else@example.com")
        self.exchange = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        self.instrument = Instrument.objects.create(
            symbol="AAPL", name="Apple Inc.", asset_type=AssetType.EQUITY,
            exchange=self.exchange, currency="USD",
        )
        self.job = IngestionJob.objects.create(
            created_by=self.user, source_type="csv",
            rows_received=100, rows_accepted=90, rows_rejected=10,
        )
        PriceRecord.objects.create(
            instrument=self.instrument, trading_date=date(2024, 3, 11),
            open="100", high="105", low="95", close="100", volume=1000,
            source="test", ingestion_job=self.job,
        )
        # Another user's data must not appear in this user's statistics.
        other_job = IngestionJob.objects.create(
            created_by=self.other, source_type="json", rows_received=50
        )
        PriceRecord.objects.create(
            instrument=self.instrument, trading_date=date(2024, 3, 12),
            open="100", high="105", low="95", close="100", volume=1000,
            source="test", ingestion_job=other_job,
        )

    def test_the_usage_summary_is_returned(self):
        response = self.get_as(self.user, USAGE_URL)

        self.assertEqual(response.status_code, 200)
        jobs = response.data["jobs"]
        self.assertEqual(jobs["total"], 1)
        self.assertEqual(jobs["rows_received"], 100)
        self.assertEqual(jobs["by_status"], {"pending": 1})
        self.assertEqual(response.data["prices"]["records_imported"], 1)

    def test_statistics_are_scoped_to_the_caller(self):
        response = self.get_as(self.other, USAGE_URL)

        self.assertEqual(response.data["jobs"]["total"], 1)
        self.assertEqual(response.data["jobs"]["rows_received"], 50)

    def test_a_user_with_no_activity_sees_zeroes(self):
        empty = self.create_user("empty@example.com")
        response = self.get_as(empty, USAGE_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["jobs"]["total"], 0)
        self.assertEqual(response.data["jobs"]["by_status"], {})
        self.assertEqual(response.data["prices"]["records_imported"], 0)

    def test_usage_requires_authentication(self):
        self.assertEqual(self.client.get(USAGE_URL).status_code, 401)

    def test_the_earliest_and_latest_price_dates_are_reported(self):
        PriceRecord.objects.create(
            instrument=self.instrument, trading_date=date(2024, 3, 20),
            open="100", high="105", low="95", close="100", volume=1000,
            source="test", ingestion_job=self.job,
        )
        # .json() is the rendered body, where dates become ISO strings.
        payload = self.get_as(self.user, USAGE_URL).json()

        self.assertEqual(payload["prices"]["earliest_date"], "2024-03-11")
        self.assertEqual(payload["prices"]["latest_date"], "2024-03-20")
