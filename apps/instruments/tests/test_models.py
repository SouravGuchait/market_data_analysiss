from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from apps.instruments.models import AssetType, Exchange, Instrument
from apps.marketdata.models import PriceRecord


def create_exchange(code="NASDAQ"):
    return Exchange.objects.create(
        code=code, name=f"{code} Exchange", timezone="America/New_York"
    )


class ExchangeTests(TestCase):
    def test_code_is_unique(self):
        create_exchange()

        with self.assertRaises(IntegrityError):
            create_exchange()

    def test_str_shows_the_code(self):
        self.assertEqual(str(create_exchange()), "NASDAQ")


class InstrumentTests(TestCase):
    def setUp(self):
        self.exchange = create_exchange()
        self.instrument = Instrument.objects.create(
            symbol="AAPL",
            name="Apple Inc.",
            asset_type=AssetType.EQUITY,
            exchange=self.exchange,
            currency="USD",
        )

    def test_symbol_is_the_string_representation(self):
        self.assertEqual(str(self.instrument), "AAPL")

    def test_an_instrument_references_its_exchange(self):
        self.assertEqual(self.instrument.exchange, self.exchange)
        self.assertEqual(self.exchange.instruments.get(), self.instrument)

    def test_symbol_must_be_unique(self):
        with self.assertRaises(IntegrityError):
            Instrument.objects.create(
                symbol="AAPL",
                name="Other Co.",
                asset_type=AssetType.EQUITY,
                exchange=self.exchange,
                currency="USD",
            )

    def test_an_unknown_asset_type_is_rejected_by_full_clean(self):
        instrument = Instrument(
            symbol="XYZ",
            name="X",
            asset_type="not-a-type",
            exchange=self.exchange,
            currency="USD",
        )

        with self.assertRaises(ValidationError) as ctx:
            instrument.full_clean()

        self.assertIn("asset_type", ctx.exception.error_dict)

    def test_is_active_defaults_to_true(self):
        self.assertTrue(self.instrument.is_active)

    def test_created_at_is_populated_automatically(self):
        self.assertIsNotNone(self.instrument.created_at)

    def test_deleting_an_exchange_with_instruments_is_protected(self):
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.exchange.delete()

    def test_prices_are_reachable_through_the_instrument(self):
        PriceRecord.objects.create(
            instrument=self.instrument,
            trading_date="2024-03-11",
            open="100",
            high="105",
            low="95",
            close="100",
            volume=1000,
            source="test",
        )

        self.assertEqual(self.instrument.prices.count(), 1)
