"""Tests for ``/api/v1/stocks/{symbol}/quality/``."""

from datetime import date

from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.instruments.models import AssetType, Exchange, Instrument
from apps.marketdata.models import PriceRecord, TradingCalendar

DAY1 = date(2024, 3, 11)
DAY2 = date(2024, 3, 12)
DAY3 = date(2024, 3, 13)
DAY4 = date(2024, 3, 14)


def add_price(instrument, trading_date, close="100"):
    return PriceRecord.objects.create(
        instrument=instrument,
        trading_date=trading_date,
        open=close, high=close, low=close, close=close,
        volume=1000, source="test",
    )


class StockQualityTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.nasdaq = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        self.aapl = Instrument.objects.create(
            symbol="AAPL", name="Apple Inc.", asset_type=AssetType.EQUITY,
            exchange=self.nasdaq, currency="USD",
        )
        add_price(self.aapl, DAY1)
        add_price(self.aapl, DAY3)
        for day in (DAY1, DAY2, DAY3):
            TradingCalendar.objects.create(date=day, exchange=self.nasdaq)
        self.url = "/api/v1/stocks/AAPL/quality/"

    def test_it_reports_the_gap_between_sessions_and_prices(self):
        response = self.get_as(self.user, self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["symbol"], "AAPL")
        self.assertEqual(response.data["observations"], 2)
        self.assertEqual(response.data["sessions"], 3)
        self.assertEqual(response.data["missing_sessions"], 1)
        self.assertEqual(response.data["missing_dates"], ["2024-03-12"])
        self.assertEqual(response.data["coverage"], "0.66666667")
        self.assertEqual(response.data["first_date"], "2024-03-11")
        self.assertEqual(response.data["last_date"], "2024-03-13")

    def test_an_explicit_range_bounds_the_review(self):
        response = self.get_as(
            self.user, f"{self.url}?start_date=2024-03-12&end_date=2024-03-13"
        )

        self.assertEqual(response.data["observations"], 1)
        self.assertEqual(response.data["sessions"], 2)
        self.assertEqual(response.data["missing_dates"], ["2024-03-12"])

    def test_without_a_calendar_coverage_is_unknown(self):
        TradingCalendar.objects.all().delete()

        response = self.get_as(self.user, self.url)

        self.assertEqual(response.data["sessions"], 0)
        self.assertIsNone(response.data["coverage"])
        self.assertEqual(response.data["missing_dates"], [])

    def test_an_instrument_with_no_prices_reports_zeros(self):
        Instrument.objects.create(
            symbol="MSFT", name="Microsoft", asset_type=AssetType.EQUITY,
            exchange=self.nasdaq, currency="USD",
        )

        response = self.get_as(self.user, "/api/v1/stocks/MSFT/quality/")

        self.assertEqual(response.data["observations"], 0)
        self.assertIsNone(response.data["start_date"])
        self.assertIsNone(response.data["first_date"])
        self.assertEqual(response.data["missing_dates"], [])

    def test_a_calendar_row_scoped_to_another_exchange_is_ignored(self):
        nyse = Exchange.objects.create(
            code="NYSE", name="New York Stock Exchange", timezone="America/New_York"
        )
        # DAY4 is not a NASDAQ session; scoping must keep it out of AAPL's review.
        TradingCalendar.objects.create(date=DAY4, exchange=nyse)

        response = self.get_as(self.user, self.url)

        self.assertEqual(response.data["sessions"], 3)
        self.assertEqual(response.data["missing_dates"], ["2024-03-12"])

    def test_a_malformed_date_is_400(self):
        response = self.get_as(self.user, f"{self.url}?start_date=11/03/2024")

        self.assertEqual(response.status_code, 400)
        self.assertIn("date_range", response.data["error"]["fields"])

    def test_an_unknown_symbol_is_404(self):
        self.assertEqual(self.get_as(self.user, "/api/v1/stocks/NOPE/quality/").status_code, 404)

    def test_anonymous_access_is_401(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
