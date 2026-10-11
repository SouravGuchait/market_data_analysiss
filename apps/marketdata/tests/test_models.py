"""Model-level tests: the constraints that enforce data integrity at the database."""

from datetime import date
from decimal import Decimal

from django.db import IntegrityError, models
from django.db.models import ProtectedError
from django.test import TestCase

from apps.ingestion.models import IngestionJob
from apps.instruments.models import Instrument
from apps.marketdata.models import PriceRecord, TradingCalendar

from .helpers import add_price, create_exchange, create_instrument, load_sample_series


def a_price(instrument, **overrides):
    """A valid price that individual tests can override one field of."""
    defaults = {
        "trading_date": date(2024, 3, 11),
        "open_": "100",
        "high": "105",
        "low": "95",
        "close": "100",
        "volume": 1000,
    }
    defaults.update(overrides)
    return add_price(instrument, **defaults)


class PriceRecordConstraintTests(TestCase):
    def setUp(self):
        self.instrument = create_instrument("AAPL")

    def test_a_price_stores_exact_decimals(self):
        a_price(self.instrument, close="100")

        # Refetched, not the in-memory instance: DecimalField converts on
        # from_db_value, so the instance you create still holds the raw input.
        stored = PriceRecord.objects.get(instrument=self.instrument)

        self.assertEqual(stored.close, Decimal("100"))
        self.assertEqual(stored.volume, 1000)

    def test_the_in_memory_instance_keeps_the_raw_input_until_refetched(self):
        price = a_price(self.instrument, close="100")

        self.assertEqual(price.close, "100")  # the value you passed

        price.refresh_from_db()

        self.assertEqual(price.close, Decimal("100"))  # as the database stored it

    def test_duplicate_instrument_and_date_is_rejected(self):
        a_price(self.instrument)

        with self.assertRaises(IntegrityError):
            a_price(self.instrument, close="101")

    def test_the_same_date_for_a_different_instrument_is_allowed(self):
        a_price(self.instrument)
        a_price(create_instrument("MSFT"), close="200")

        self.assertEqual(PriceRecord.objects.count(), 2)

    def test_high_below_low_is_rejected_by_the_database(self):
        with self.assertRaises(IntegrityError):
            a_price(self.instrument, high="90", low="95")

    def test_negative_volume_is_rejected_by_the_database(self):
        # No validator catches this: the CheckConstraint does, which also covers
        # bulk_create, where validators never run.
        with self.assertRaises(IntegrityError):
            a_price(self.instrument, volume=-100)

    def test_adjusted_close_may_be_null_because_it_is_sometimes_unavailable(self):
        self.assertIsNone(a_price(self.instrument).adjusted_close)

    def test_adjusted_close_is_stored_when_supplied(self):
        a_price(self.instrument, adjusted_close="98")

        stored = PriceRecord.objects.get(instrument=self.instrument)

        self.assertEqual(stored.adjusted_close, Decimal("98"))

    def test_deleting_an_instrument_with_prices_is_protected(self):
        a_price(self.instrument)

        with self.assertRaises(ProtectedError):
            self.instrument.delete()

    def test_deleting_an_exchange_with_instruments_is_protected(self):
        with self.assertRaises(ProtectedError):
            self.instrument.exchange.delete()

    def test_deleting_an_instrument_without_prices_is_allowed(self):
        a_price(create_instrument("MSFT"))
        pk = self.instrument.pk

        self.instrument.delete()

        # delete() clears the instance's pk, so the existence check is by
        # captured pk rather than by the instance.
        self.assertFalse(Instrument.objects.filter(pk=pk).exists())
        self.assertEqual(PriceRecord.objects.filter(instrument_id=pk).count(), 0)

    def test_the_related_name_exposes_prices_from_the_instrument(self):
        a_price(self.instrument)

        self.assertEqual(self.instrument.prices.count(), 1)

    def test_the_unique_constraint_and_indexes_are_named(self):
        self.assertIn(
            "uniq_price_instrument_trading_date",
            {c.name for c in PriceRecord._meta.constraints},
        )
        self.assertIn(
            "idx_price_trading_date", {i.name for i in PriceRecord._meta.indexes}
        )


class IngestionJobLinkTests(TestCase):
    def setUp(self):
        self.instrument = create_instrument("AAPL")
        self.user = self._create_user()
        self.job = IngestionJob.objects.create(created_by=self.user, source_type="csv")

    def _create_user(self):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(
            email="jobowner@example.com", password="drf-portfolio-42!"
        )

    def test_a_price_can_record_the_job_that_imported_it(self):
        price = a_price(self.instrument, ingestion_job=self.job)

        self.assertEqual(price.ingestion_job, self.job)
        self.assertEqual(self.job.prices.count(), 1)

    def test_a_price_can_exist_without_a_job(self):
        price = a_price(self.instrument)

        self.assertIsNone(price.ingestion_job)


class DuplicateAuditTests(TestCase):
    """The query you would run against a staging table before loading."""

    def test_the_audit_query_finds_no_duplicate_instrument_dates(self):
        load_sample_series()

        duplicates = (
            PriceRecord.objects.values("instrument", "trading_date")
            .annotate(row_count=models.Count("id"))
            .filter(row_count__gt=1)
        )

        self.assertEqual(list(duplicates), [])

    def test_the_sample_contains_fifteen_rows(self):
        load_sample_series()

        self.assertEqual(PriceRecord.objects.count(), 15)
        self.assertEqual(PriceRecord.objects.distinct().count(), 15)
