"""Tests for the price-history endpoint."""

from datetime import date

from django.test import TestCase

from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.instruments.models import AssetType, Exchange, Instrument
from apps.marketdata import services
from apps.marketdata.models import PriceRecord

DAY1 = date(2024, 3, 11)
DAY2 = date(2024, 3, 12)
DAY3 = date(2024, 3, 13)


def add_price(instrument, trading_date, close, volume=1000):
    return PriceRecord.objects.create(
        instrument=instrument,
        trading_date=trading_date,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        source="test",
    )


class PriceServiceTests(TestCase):
    def setUp(self):
        self.exchange = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        self.instrument = Instrument.objects.create(
            symbol="AAPL", name="Apple Inc.", asset_type=AssetType.EQUITY,
            exchange=self.exchange, currency="USD",
        )
        add_price(self.instrument, DAY1, "100")
        add_price(self.instrument, DAY2, "102")
        add_price(self.instrument, DAY3, "104")

    def test_the_default_order_is_chronological(self):
        prices = services.get_prices_for_instrument(self.instrument)

        self.assertEqual(list(prices.values_list("trading_date", flat=True)), [DAY1, DAY2, DAY3])

    def test_a_start_date_is_inclusive(self):
        prices = services.get_prices_for_instrument(self.instrument, start_date=DAY2)

        self.assertEqual(list(prices.values_list("trading_date", flat=True)), [DAY2, DAY3])

    def test_an_end_date_is_inclusive(self):
        prices = services.get_prices_for_instrument(self.instrument, end_date=DAY2)

        self.assertEqual(list(prices.values_list("trading_date", flat=True)), [DAY1, DAY2])

    def test_the_range_bounds_are_respected(self):
        prices = services.get_prices_for_instrument(
            self.instrument, start_date=DAY2, end_date=DAY2
        )

        self.assertEqual([p.trading_date for p in prices], [DAY2])

    def test_parse_date_handles_empty_values(self):
        self.assertIsNone(services.parse_date(None))
        self.assertIsNone(services.parse_date(""))
        self.assertEqual(services.parse_date("2024-03-11"), DAY1)

    def test_parse_date_rejects_a_malformed_value(self):
        with self.assertRaises(ValueError):
            services.parse_date("11/03/2024")

    def test_an_inverted_range_is_rejected(self):
        with self.assertRaises(ValueError):
            services.validate_date_range(DAY3, DAY1)

    def test_a_valid_range_is_returned_unchanged(self):
        self.assertEqual(services.validate_date_range(DAY1, DAY3), (DAY1, DAY3))

    def test_the_summary_aggregates_in_sql(self):
        summary = services.get_price_summary(
            services.get_prices_for_instrument(self.instrument)
        )

        self.assertEqual(summary["record_count"], 3)
        self.assertEqual(summary["first_trading_date"], DAY1)
        self.assertEqual(summary["last_trading_date"], DAY3)

    def test_the_queryset_joins_the_instrument_and_exchange(self):
        with self.assertNumQueries(1):
            instrument = list(services.get_price_queryset())[0].instrument.exchange.code
            self.assertEqual(instrument, "NASDAQ")


class PriceHistoryAPITests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.exchange = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        self.instrument = Instrument.objects.create(
            symbol="AAPL", name="Apple Inc.", asset_type=AssetType.EQUITY,
            exchange=self.exchange, currency="USD",
        )
        for day, close in ((DAY1, "100"), (DAY2, "102"), (DAY3, "104")):
            add_price(self.instrument, day, close)
        self.url = f"/api/v1/market-data/instruments/{self.instrument.pk}/prices/"

    def test_returns_the_full_history(self):
        response = self.get_as(self.user, self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(response.data["results"][0]["trading_date"], "2024-03-11")

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_a_missing_instrument_is_404(self):
        response = self.get_as(self.user, "/api/v1/market-data/instruments/999/prices/")

        self.assertEqual(response.status_code, 404)

    def test_a_date_range_limits_the_results(self):
        response = self.get_as(
            self.user, f"{self.url}?start_date=2024-03-12&end_date=2024-03-12"
        )

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["trading_date"], "2024-03-12")

    def test_a_start_date_alone_is_supported(self):
        response = self.get_as(self.user, f"{self.url}?start_date=2024-03-13")

        self.assertEqual(response.data["count"], 1)

    def test_an_end_date_alone_is_supported(self):
        response = self.get_as(self.user, f"{self.url}?end_date=2024-03-12")

        self.assertEqual(response.data["count"], 2)

    def test_a_malformed_date_is_400_not_500(self):
        response = self.get_as(self.user, f"{self.url}?start_date=11/03/2024")

        self.assertEqual(response.status_code, 400)
        self.assertIn("date_range", response.data["error"]["fields"])

    def test_an_inverted_range_is_400(self):
        response = self.get_as(
            self.user, f"{self.url}?start_date=2024-03-13&end_date=2024-03-11"
        )

        self.assertEqual(response.status_code, 400)

    def test_ordering_descending(self):
        response = self.get_as(self.user, f"{self.url}?ordering=-trading_date")

        self.assertEqual(response.data["results"][0]["trading_date"], "2024-03-13")

    def test_pagination_applies(self):
        for day in range(14, 40):
            add_price(self.instrument, date(2024, 4, day) if day <= 30 else date(2024, 5, day - 30), "100")

        response = self.get_as(self.user, f"{self.url}?page_size=10")

        self.assertEqual(len(response.data["results"]), 10)
        self.assertEqual(response.data["count"], 29)

    def test_the_payload_contains_only_price_fields(self):
        response = self.get_as(self.user, self.url)

        self.assertEqual(
            sorted(response.data["results"][0].keys()),
            ["adjusted_close", "close", "high", "low", "open", "source", "trading_date", "volume"],
        )

    def test_an_empty_history_returns_an_empty_page_not_an_error(self):
        other = Instrument.objects.create(
            symbol="MSFT", name="Microsoft", asset_type=AssetType.EQUITY,
            exchange=self.exchange, currency="USD",
        )
        response = self.get_as(
            self.user, f"/api/v1/market-data/instruments/{other.pk}/prices/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(response.data["results"], [])

    def test_avoids_the_n1_query_for_instrument_and_exchange(self):
        # Four queries total, and the count does not grow with the page size:
        # the JWT user, the instrument existence check, the paginator's COUNT,
        # and one JOIN for the page itself.
        with self.assertNumQueries(4):
            response = self.get_as(self.user, self.url)
            [row["close"] for row in response.data["results"]]
