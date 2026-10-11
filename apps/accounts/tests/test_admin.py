"""Admin UI coverage.

The requirement was that an administrator can manage roles from Django admin, so
these tests assert each page actually renders for a staff user - not just that
the model is registered.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.analytics.models import AnalyticSnapshot
from apps.audit.models import AuditEvent
from apps.ingestion.models import IngestionFile, IngestionJob, IngestionValidationError
from apps.instruments.models import AssetType, Exchange, Instrument
from apps.marketdata.models import PriceRecord

from apps.accounts.models import Role

PASSWORD = "drf-portfolio-42!"


class AdminSetupMixin:
    def setUp(self):
        super().setUp()
        self.admin_user = get_user_model().objects.create_superuser(
            email="admin@example.com", password=PASSWORD
        )
        self.client.force_login(self.admin_user)

    def assert_admin_page_loads(self, changelist_url_path):
        response = self.client.get(changelist_url_path)
        self.assertEqual(response.status_code, 200)


class UserAdminTests(AdminSetupMixin, TestCase):
    def test_the_user_changelist_loads(self):
        self.assert_admin_page_loads(reverse("admin:accounts_user_changelist"))

    def test_the_user_change_form_renders_the_role_field(self):
        response = self.client.get(
            reverse("admin:accounts_user_change", args=[self.admin_user.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "role")

    def test_the_add_user_form_uses_email_not_username(self):
        response = self.client.get(reverse("admin:accounts_user_add"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="email"')
        self.assertNotContains(response, 'name="username"')

    def test_role_is_filterable_in_the_changelist(self):
        response = self.client.get(reverse("admin:accounts_user_changelist"))

        self.assertContains(response, "role")

    def test_an_analyst_can_be_promoted_to_administrator_through_the_admin(self):
        analyst = get_user_model().objects.create_user(
            email="analyst@example.com", password=PASSWORD
        )
        self.assertEqual(analyst.role, Role.ANALYST)

        response = self.client.post(
            reverse("admin:accounts_user_change", args=[analyst.pk]),
            {
                "email": "analyst@example.com",
                "role": Role.ADMINISTRATOR,
                "is_active": "on",
                "is_staff": "on",
                "is_superuser": "",
                "date_joined_0": "2026-01-01",
                "date_joined_1": "00:00:00",
                "last_login_0": "",
                "last_login_1": "",
                "initial-date_joined_0": "2026-01-01",
                "initial-date_joined_1": "00:00:00",
            },
        )

        self.assertEqual(response.status_code, 302)
        analyst.refresh_from_db()
        self.assertEqual(analyst.role, Role.ADMINISTRATOR)

    def test_a_non_admin_cannot_reach_the_admin(self):
        self.client.logout()
        analyst = get_user_model().objects.create_user(
            email="analyst@example.com", password=PASSWORD
        )
        self.client.force_login(analyst)

        response = self.client.get(reverse("admin:accounts_user_changelist"))

        self.assertEqual(response.status_code, 302)


class MarketDataAdminTests(AdminSetupMixin, TestCase):
    def setUp(self):
        super().setUp()
        exchange = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        self.instrument = Instrument.objects.create(
            symbol="AAPL",
            name="Apple Inc.",
            asset_type=AssetType.EQUITY,
            exchange=exchange,
            currency="USD",
        )
        self.job = IngestionJob.objects.create(
            created_by=self.admin_user, source_type="csv"
        )

    def test_every_changelist_renders(self):
        urls = [
            "admin:instruments_exchange_changelist",
            "admin:instruments_instrument_changelist",
            "admin:marketdata_pricerecord_changelist",
            "admin:marketdata_tradingcalendar_changelist",
            "admin:ingestion_ingestionjob_changelist",
            "admin:ingestion_ingestionfile_changelist",
            "admin:ingestion_ingestionvalidationerror_changelist",
            "admin:analytics_analyticsnapshot_changelist",
            "admin:audit_auditevent_changelist",
        ]
        for name in urls:
            with self.subTest(name=name):
                self.assert_admin_page_loads(reverse(name))

    def test_the_job_change_form_shows_its_files_and_validation_errors(self):
        IngestionFile.objects.create(
            job=self.job,
            original_filename="prices.csv",
            storage_path="ingestion/prices.csv",
            checksum="a" * 64,
            content_type="text/csv",
        )
        IngestionValidationError.objects.create(
            job=self.job, row_number=7, field_name="close", error_code="too_high"
        )

        response = self.client.get(
            reverse("admin:ingestion_ingestionjob_change", args=[self.job.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "prices.csv")
        self.assertContains(response, "too_high")

    def test_audit_events_cannot_be_added_or_edited_by_hand(self):
        # has_add_permission returns False, so the add view is refused outright.
        self.assertEqual(
            self.client.get(reverse("admin:audit_auditevent_add")).status_code, 403
        )
        response = self.client.get(reverse("admin:audit_auditevent_changelist"))
        self.assertNotContains(response, "Add audit event")

    def test_a_price_record_can_be_created_through_the_admin(self):
        response = self.client.post(
            reverse("admin:marketdata_pricerecord_add"),
            {
                "instrument": self.instrument.pk,
                "trading_date": "2024-03-11",
                "open": "100.00000000",
                "high": "105.00000000",
                "low": "95.00000000",
                "close": "100.00000000",
                "volume": 1000,
                "source": "admin",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(PriceRecord.objects.count(), 1)

    def test_an_analytics_snapshot_can_be_created_through_the_admin(self):
        response = self.client.post(
            reverse("admin:analytics_analyticsnapshot_add"),
            {
                "instrument": self.instrument.pk,
                "as_of_date": "2024-03-15",
                "metric_type": "return",
                "lookback_days": 30,
                "value": "0.08000000",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(AnalyticSnapshot.objects.count(), 1)

    def test_an_audit_event_is_readonly_once_created(self):
        event = AuditEvent.objects.create(
            user=self.admin_user, action="test.action", request_id="req-1"
        )

        response = self.client.get(
            reverse("admin:audit_auditevent_change", args=[event.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "created_at")
