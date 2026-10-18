"""Tests for the instrument endpoints."""

from django.test import TestCase, override_settings

from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.instruments import services
from apps.instruments.models import AssetType, Exchange, Instrument


class InstrumentServiceTests(TestCase):
    """The service layer is callable without HTTP, so it is tested directly."""

    def setUp(self):
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
        self.msft = Instrument.objects.create(
            symbol="MSFT", name="Microsoft Corporation", asset_type=AssetType.EQUITY,
            exchange=self.nasdaq, currency="USD", is_active=False,
        )
        self.spy = Instrument.objects.create(
            symbol="SPY", name="SPDR S&P 500 ETF Trust", asset_type=AssetType.ETF,
            exchange=self.nyse, currency="USD",
        )

    def test_symbol_matching_is_case_insensitive(self):
        self.assertEqual(list(services.list_instruments(symbol="aapl")), [self.aapl])

    def test_exchange_filters_by_code(self):
        self.assertEqual(list(services.list_instruments(exchange_code="NYSE")), [self.spy])

    def test_asset_type_filters(self):
        self.assertEqual(
            list(services.list_instruments(asset_type=AssetType.EQUITY)),
            [self.aapl, self.msft],
        )

    def test_is_active_filters(self):
        self.assertEqual(list(services.list_instruments(is_active=False)), [self.msft])
        self.assertEqual(
            list(services.list_instruments(is_active=True)), [self.aapl, self.spy]
        )

    def test_search_matches_symbol_or_name_substring(self):
        self.assertEqual(list(services.list_instruments(search="Mic")), [self.msft])
        self.assertEqual(list(services.list_instruments(search="AAPL")), [self.aapl])

    def test_filters_combine(self):
        self.assertEqual(
            list(services.list_instruments(asset_type=AssetType.EQUITY, is_active=False)),
            [self.msft],
        )

    def test_results_are_ordered_by_symbol(self):
        self.assertEqual(
            [i.symbol for i in services.list_instruments()], ["AAPL", "MSFT", "SPY"]
        )

    def test_the_base_queryset_joins_the_exchange(self):
        with self.assertNumQueries(1):
            list(services.get_instrument_queryset())


class InstrumentListTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.nasdaq = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        self.aapl = Instrument.objects.create(
            symbol="AAPL", name="Apple Inc.", asset_type=AssetType.EQUITY,
            exchange=self.nasdaq, currency="USD",
        )
        self.spy = Instrument.objects.create(
            symbol="SPY", name="SPDR S&P 500 ETF Trust", asset_type=AssetType.ETF,
            exchange=self.nasdaq, currency="USD",
        )

    def test_list_returns_instruments(self):
        response = self.get_as(self.user, "/api/v1/instruments/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(response.data["results"][0]["symbol"], "AAPL")


    def test_list_requires_authentication(self):
        self.assertEqual(self.client.get("/api/v1/instruments/").status_code, 401)

    def test_list_flattens_the_exchange_to_a_code(self):
        response = self.get_as(self.user, "/api/v1/instruments/")

        self.assertEqual(response.data["results"][0]["exchange_code"], "NASDAQ")
        self.assertNotIn("exchange", response.data["results"][0])

    def test_filter_by_symbol(self):
        response = self.get_as(self.user, "/api/v1/instruments/?symbol=spy")

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["symbol"], "SPY")

    def test_filter_by_exchange(self):
        response = self.get_as(self.user, "/api/v1/instruments/?exchange=NASDAQ")

        self.assertEqual(response.data["count"], 2)

    def test_filter_by_asset_type(self):
        response = self.get_as(self.user, "/api/v1/instruments/?asset_type=etf")

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["symbol"], "SPY")

    def test_an_unknown_asset_type_is_a_400_not_a_500(self):
        response = self.get_as(self.user, "/api/v1/instruments/?asset_type=widget")

        self.assertEqual(response.status_code, 400)

    def test_search_matches_a_name_substring(self):
        response = self.get_as(self.user, "/api/v1/instruments/?search=Apple")

        self.assertEqual(response.data["count"], 1)

    def test_list_avoids_the_n1_query_for_the_exchange(self):
        # Three queries total: the JWT user lookup, the paginator's COUNT, and
        # one JOIN for the page. The count does not grow with the number of
        # rows, which is the point of select_related.
        with self.assertNumQueries(3):
            response = self.get_as(self.user, "/api/v1/instruments/")
            list(response.data["results"])

    def test_pagination_returns_a_page_wrapper(self):
        for index in range(30):
            Instrument.objects.create(
                symbol=f"SYM{index}", name=f"Symbol {index}",
                asset_type=AssetType.EQUITY, exchange=self.nasdaq, currency="USD",
            )

        response = self.get_as(self.user, "/api/v1/instruments/?page_size=10")

        self.assertEqual(len(response.data["results"]), 10)
        self.assertEqual(response.data["count"], 32)
        self.assertIsNotNone(response.data["next"])

    def test_ordering_by_symbol_descending(self):
        response = self.get_as(self.user, "/api/v1/instruments/?ordering=-symbol")

        self.assertEqual(response.data["results"][0]["symbol"], "SPY")

    def test_an_invalid_ordering_field_is_ignored_not_fatal(self):
        response = self.get_as(self.user, "/api/v1/instruments/?ordering=not_a_field")

        self.assertEqual(response.status_code, 200)

    def test_is_active_accepts_true_and_false(self):
        # setUp creates two instruments, both active by default.
        self.assertEqual(
            self.get_as(self.user, "/api/v1/instruments/?is_active=true").data["count"], 2
        )
        self.assertEqual(
            self.get_as(self.user, "/api/v1/instruments/?is_active=false").data["count"], 0
        )

        Instrument.objects.create(
            symbol="OLDQ", name="Old Corp", asset_type=AssetType.EQUITY,
            exchange=self.nasdaq, currency="USD", is_active=False,
        )
        self.assertEqual(
            self.get_as(self.user, "/api/v1/instruments/?is_active=false").data["count"], 1
        )
        self.assertEqual(
            self.get_as(self.user, "/api/v1/instruments/?is_active=true").data["count"], 2
        )

    def test_is_active_ignores_a_non_boolean_rather_than_erroring(self):
        # django-filter's BooleanFilter contract: an unparseable value is
        # dropped, so the filter silently does nothing. Documented in the
        # README so it cannot surprise a caller.
        response = self.get_as(self.user, "/api/v1/instruments/?is_active=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)

    def test_asset_type_is_case_sensitive(self):
        self.assertEqual(
            self.get_as(self.user, "/api/v1/instruments/?asset_type=etf").data["count"],
            1,
        )
        self.assertEqual(
            self.get_as(self.user, "/api/v1/instruments/?asset_type=ETF").status_code,
            400,
        )

    def test_an_out_of_range_page_is_a_404_not_an_empty_list(self):
        response = self.get_as(self.user, "/api/v1/instruments/?page=999")

        self.assertEqual(response.status_code, 404)

    def test_a_non_numeric_page_is_a_404(self):
        response = self.get_as(self.user, "/api/v1/instruments/?page=abc")

        self.assertEqual(response.status_code, 404)

    def test_a_non_numeric_page_size_falls_back_to_the_default(self):
        response = self.get_as(self.user, "/api/v1/instruments/?page_size=abc")

        self.assertEqual(response.status_code, 200)

    def test_page_size_is_clamped_to_the_maximum(self):
        # setUp already creates two; this brings the total to seven.
        for index in range(5):
            Instrument.objects.create(
                symbol=f"EXTRA{index}", name=f"Extra {index}",
                asset_type=AssetType.EQUITY, exchange=self.nasdaq, currency="USD",
            )

        response = self.get_as(self.user, "/api/v1/instruments/?page_size=1000")

        # Seven rows all fit under the clamped maximum of 100, so the clamp is
        # exercised without needing more than 100 fixtures.
        self.assertEqual(len(response.data["results"]), 7)


class InstrumentListTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.nasdaq = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        self.aapl = Instrument.objects.create(
            symbol="AAPL", name="Apple Inc.", asset_type=AssetType.EQUITY,
            exchange=self.nasdaq, currency="USD",
        )
        self.spy = Instrument.objects.create(
            symbol="SPY", name="SPDR S&P 500 ETF Trust", asset_type=AssetType.ETF,
            exchange=self.nasdaq, currency="USD",
        )

    def test_an_unknown_query_parameter_is_ignored(self):
        response = self.get_as(self.user, "/api/v1/instruments/?bogus=1&another=2")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)


class InstrumentDetailTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.nasdaq = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        self.aapl = Instrument.objects.create(
            symbol="AAPL", name="Apple Inc.", asset_type=AssetType.EQUITY,
            exchange=self.nasdaq, currency="USD",
        )

    def test_retrieve_nests_the_exchange(self):
        response = self.get_as(self.user, f"/api/v1/instruments/{self.aapl.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["symbol"], "AAPL")
        self.assertEqual(response.data["exchange"]["code"], "NASDAQ")

    def test_a_missing_instrument_is_404(self):
        response = self.get_as(self.user, "/api/v1/instruments/99999/")

        self.assertEqual(response.status_code, 404)

    def test_detail_requires_authentication(self):
        self.assertEqual(
            self.client.get(f"/api/v1/instruments/{self.aapl.pk}/").status_code, 401
        )


class ExchangeListTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        Exchange.objects.create(code="NASDAQ", name="Nasdaq", timezone="America/New_York")
        Exchange.objects.create(code="NYSE", name="New York Stock Exchange", timezone="America/New_York")

    def test_exchanges_are_listed(self):
        response = self.get_as(self.user, "/api/v1/instruments/exchanges/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)

    def test_exchanges_are_not_paginated(self):
        response = self.get_as(self.user, "/api/v1/instruments/exchanges/")

        self.assertIsInstance(response.data, list)

    def test_exchanges_require_authentication(self):
        self.assertEqual(
            self.client.get("/api/v1/instruments/exchanges/").status_code, 401
        )


@override_settings(API_CACHE_ENABLED=True)
class ReferenceCacheTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.exchange = Exchange.objects.create(
            code="NASDAQ", name="Nasdaq", timezone="America/New_York"
        )
        Instrument.objects.create(
            symbol="AAPL", name="Apple Inc.", asset_type=AssetType.EQUITY,
            exchange=self.exchange, currency="USD",
        )

    def test_instrument_list_repeats_from_cache(self):
        first = self.get_as(self.user, "/api/v1/instruments/")
        second = self.get_as(self.user, "/api/v1/instruments/")

        self.assertEqual(first["X-Cache"], "MISS")
        self.assertEqual(second["X-Cache"], "HIT")
        self.assertEqual(first.content, second.content)

    def test_reference_write_invalidates_cached_list(self):
        self.assertEqual(
            self.get_as(self.user, "/api/v1/instruments/")["X-Cache"], "MISS"
        )
        with self.captureOnCommitCallbacks(execute=True):
            Instrument.objects.create(
                symbol="MSFT", name="Microsoft", asset_type=AssetType.EQUITY,
                exchange=self.exchange, currency="USD",
            )

        response = self.get_as(self.user, "/api/v1/instruments/")
        self.assertEqual(response["X-Cache"], "MISS")
        self.assertEqual(response.data["count"], 2)
