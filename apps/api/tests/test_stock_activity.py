"""Tests for ``/api/v1/stocks/{symbol}/history/`` and ``.../analytics/``."""

from datetime import date

from apps.analytics.models import AnalyticSnapshot, MetricType
from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.instruments.models import AssetType, Exchange, Instrument
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


class StockSubResourceSetup(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.exchange = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        self.aapl = Instrument.objects.create(
            symbol="AAPL", name="Apple Inc.", asset_type=AssetType.EQUITY,
            exchange=self.exchange, currency="USD",
        )


class StockHistoryTests(StockSubResourceSetup):
    def setUp(self):
        super().setUp()
        for day, close in ((DAY1, "100"), (DAY2, "102"), (DAY3, "104")):
            add_price(self.aapl, day, close)
        self.url = "/api/v1/stocks/AAPL/history/"

    def test_returns_the_full_history(self):
        response = self.get_as(self.user, self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(response.data["results"][0]["trading_date"], "2024-03-11")

    def test_the_symbol_is_case_insensitive(self):
        self.assertEqual(self.get_as(self.user, "/api/v1/stocks/aapl/history/").status_code, 200)

    def test_a_date_range_limits_the_results(self):
        response = self.get_as(
            self.user, f"{self.url}?start_date=2024-03-12&end_date=2024-03-12"
        )

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["trading_date"], "2024-03-12")

    def test_a_malformed_date_is_400(self):
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
        for index in range(20):
            add_price(self.aapl, date(2024, 4, index + 1), "100")

        response = self.get_as(self.user, f"{self.url}?page_size=10")

        self.assertEqual(len(response.data["results"]), 10)
        self.assertEqual(response.data["count"], 23)

    def test_an_unknown_symbol_is_404(self):
        self.assertEqual(self.get_as(self.user, "/api/v1/stocks/NOPE/history/").status_code, 404)

    def test_anonymous_access_is_401(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_avoids_the_n1_query(self):
        # JWT user, instrument lookup, paginator COUNT, one page query.
        with self.assertNumQueries(4):
            response = self.get_as(self.user, self.url)
            [row["close"] for row in response.data["results"]]


class StockAnalyticsTests(StockSubResourceSetup):
    def setUp(self):
        super().setUp()
        for day, value in ((DAY1, "0.01000000"), (DAY2, "0.02000000")):
            AnalyticSnapshot.objects.create(
                instrument=self.aapl,
                as_of_date=day,
                metric_type=MetricType.RETURN,
                lookback_days=30,
                value=value,
            )
        AnalyticSnapshot.objects.create(
            instrument=self.aapl,
            as_of_date=DAY3,
            metric_type=MetricType.VOLATILITY,
            lookback_days=30,
            value="0.15000000",
        )
        self.url = "/api/v1/stocks/AAPL/analytics/"

    def test_returns_the_snapshots_newest_first(self):
        response = self.get_as(self.user, self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(response.data["results"][0]["symbol"], "AAPL")

    def test_filtering_by_metric_type(self):
        response = self.get_as(self.user, f"{self.url}?metric_type=return")

        self.assertEqual(response.data["count"], 2)

    def test_an_unknown_metric_type_is_400(self):
        response = self.get_as(self.user, f"{self.url}?metric_type=tan")

        self.assertEqual(response.status_code, 400)
        self.assertIn("metric_type", response.data["error"]["fields"])

    def test_a_date_range_limits_the_results(self):
        response = self.get_as(self.user, f"{self.url}?start_date=2024-03-12")

        self.assertEqual(response.data["count"], 2)

    def test_ordering_ascending_by_date(self):
        response = self.get_as(self.user, f"{self.url}?ordering=as_of_date")

        self.assertEqual(response.data["results"][0]["as_of_date"], "2024-03-11")

    def test_the_payload_contains_only_snapshot_fields(self):
        response = self.get_as(self.user, self.url)

        self.assertEqual(
            sorted(response.data["results"][0].keys()),
            ["as_of_date", "calculated_at", "lookback_days", "metadata",
             "metric_type", "symbol", "value"],
        )

    def test_an_unknown_symbol_is_404(self):
        self.assertEqual(self.get_as(self.user, "/api/v1/stocks/NOPE/analytics/").status_code, 404)

    def test_anonymous_access_is_401(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
