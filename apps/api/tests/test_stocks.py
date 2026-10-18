"""Tests for ``/api/v1/stocks/`` and ``/api/v1/stocks/{symbol}/``.

Instruments are reference data every authenticated role reads, so unlike jobs
there is no per-owner scoping here: the tests assert the shared rows and the
flattened list shape instead.
"""

from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.instruments.models import AssetType, Exchange, Instrument

STOCKS_URL = "/api/v1/stocks/"


class StockListTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.nasdaq = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        self.nyse = Exchange.objects.create(
            code="NYSE", name="New York Stock Exchange", timezone="America/New_York"
        )
        self.aapl = Instrument.objects.create(
            symbol="AAPL", name="Apple Inc.", asset_type=AssetType.EQUITY,
            exchange=self.nasdaq, currency="USD",
        )
        self.spy = Instrument.objects.create(
            symbol="SPY", name="SPDR S&P 500 ETF Trust", asset_type=AssetType.ETF,
            exchange=self.nyse, currency="USD",
        )

    def test_list_returns_stocks(self):
        response = self.get_as(self.user, STOCKS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(response.data["results"][0]["symbol"], "AAPL")

    def test_list_requires_authentication(self):
        self.assertEqual(self.client.get(STOCKS_URL).status_code, 401)

    def test_the_exchange_is_flattened_to_a_code(self):
        response = self.get_as(self.user, STOCKS_URL)

        self.assertEqual(response.data["results"][0]["exchange_code"], "NASDAQ")
        self.assertNotIn("exchange", response.data["results"][0])

    def test_filter_by_symbol_is_case_insensitive(self):
        response = self.get_as(self.user, f"{STOCKS_URL}?symbol=spy")

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["symbol"], "SPY")

    def test_filter_by_exchange(self):
        response = self.get_as(self.user, f"{STOCKS_URL}?exchange=NYSE")

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["symbol"], "SPY")

    def test_filter_by_asset_type(self):
        response = self.get_as(self.user, f"{STOCKS_URL}?asset_type=etf")

        self.assertEqual(response.data["count"], 1)

    def test_an_unknown_asset_type_is_a_400(self):
        response = self.get_as(self.user, f"{STOCKS_URL}?asset_type=widget")

        self.assertEqual(response.status_code, 400)
        self.assertIn("asset_type", response.data["error"]["fields"])

    def test_search_matches_a_name_substring(self):
        response = self.get_as(self.user, f"{STOCKS_URL}?search=Apple")

        self.assertEqual(response.data["count"], 1)

    def test_ordering_descending_by_symbol(self):
        response = self.get_as(self.user, f"{STOCKS_URL}?ordering=-symbol")

        self.assertEqual(response.data["results"][0]["symbol"], "SPY")

    def test_pagination_returns_a_page_wrapper(self):
        for index in range(30):
            Instrument.objects.create(
                symbol=f"SYM{index}", name=f"Symbol {index}",
                asset_type=AssetType.EQUITY, exchange=self.nasdaq, currency="USD",
            )

        response = self.get_as(self.user, f"{STOCKS_URL}?page_size=10")

        self.assertEqual(len(response.data["results"]), 10)
        self.assertEqual(response.data["count"], 32)
        self.assertIsNotNone(response.data["next"])

    def test_an_out_of_range_page_is_a_404(self):
        self.assertEqual(self.get_as(self.user, f"{STOCKS_URL}?page=999").status_code, 404)

    def test_list_avoids_the_n1_query_for_the_exchange(self):
        # JWT user lookup, paginator COUNT, and one JOIN for the page.
        with self.assertNumQueries(3):
            response = self.get_as(self.user, STOCKS_URL)
            list(response.data["results"])


class StockDetailTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.nasdaq = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        self.aapl = Instrument.objects.create(
            symbol="AAPL", name="Apple Inc.", asset_type=AssetType.EQUITY,
            exchange=self.nasdaq, currency="USD",
        )

    def test_retrieve_by_symbol_nests_the_exchange(self):
        response = self.get_as(self.user, f"{STOCKS_URL}AAPL/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["symbol"], "AAPL")
        self.assertEqual(response.data["exchange"]["code"], "NASDAQ")

    def test_the_symbol_lookup_is_case_insensitive(self):
        response = self.get_as(self.user, f"{STOCKS_URL}aapl/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["symbol"], "AAPL")

    def test_a_missing_symbol_is_404(self):
        self.assertEqual(self.get_as(self.user, f"{STOCKS_URL}NOPE/").status_code, 404)

    def test_detail_requires_authentication(self):
        self.assertEqual(self.client.get(f"{STOCKS_URL}AAPL/").status_code, 401)

    def test_detail_joins_the_exchange_in_one_query(self):
        # JWT user lookup, then the instrument JOIN its exchange.
        with self.assertNumQueries(2):
            response = self.get_as(self.user, f"{STOCKS_URL}AAPL/")
            _ = response.data["exchange"]["code"]
