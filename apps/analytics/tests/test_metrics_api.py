"""Behaviour of the metrics endpoint.

Caching is off for the whole suite by default, so these are the *control* tests:
they state what the endpoint returns, and the cache tests assert that a cached
response is indistinguishable from these.
"""

from datetime import date

from apps.analytics.models import AnalyticSnapshot, MetricType
from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.instruments.models import AssetType, Exchange, Instrument


class MetricsAPITestCase(AuthenticatedAPITestCase):
    """A one-instrument analytics fixture, reachable at ``self.url``."""

    def setUp(self):
        super().setUp()
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
        self.url = f"/api/v1/analytics/instruments/{self.instrument.pk}/metrics/"

    def add_snapshot(self, *, day, metric=MetricType.RETURN, lookback=30, value="0.5"):
        return AnalyticSnapshot.objects.create(
            instrument=self.instrument,
            as_of_date=day,
            metric_type=metric,
            lookback_days=lookback,
            value=value,
        )


class MetricsListTests(MetricsAPITestCase):
    def test_metrics_are_listed_newest_first(self):
        self.add_snapshot(day=date(2024, 3, 12))
        self.add_snapshot(day=date(2024, 3, 14))
        self.add_snapshot(day=date(2024, 3, 13))

        response = self.get_as(self.user, self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(
            [row["as_of_date"] for row in response.data["results"]],
            ["2024-03-14", "2024-03-13", "2024-03-12"],
        )

    def test_the_metric_type_filter_selects_one_series(self):
        self.add_snapshot(day=date(2024, 3, 14), metric=MetricType.RETURN)
        self.add_snapshot(day=date(2024, 3, 14), metric=MetricType.VOLATILITY)

        response = self.get_as(self.user, f"{self.url}?metric_type=volatility")

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["metric_type"], "volatility")

    def test_the_lookback_filter_keeps_configurations_apart(self):
        """A 30-day and a 90-day average share a name but not a series."""
        self.add_snapshot(day=date(2024, 3, 14), metric=MetricType.MOVING_AVERAGE, lookback=30)
        self.add_snapshot(day=date(2024, 3, 14), metric=MetricType.MOVING_AVERAGE, lookback=90)

        response = self.get_as(self.user, f"{self.url}?lookback_days=90")

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["lookback_days"], 90)

    def test_a_date_range_is_inclusive_at_both_ends(self):
        for day in (date(2024, 3, 10), date(2024, 3, 12), date(2024, 3, 15)):
            self.add_snapshot(day=day)

        response = self.get_as(
            self.user, f"{self.url}?start_date=2024-03-12&end_date=2024-03-15"
        )

        self.assertEqual(response.data["count"], 2)

    def test_ordering_by_value_reverses_the_series(self):
        self.add_snapshot(day=date(2024, 3, 12), value="0.1")
        self.add_snapshot(day=date(2024, 3, 13), value="0.9")

        response = self.get_as(self.user, f"{self.url}?ordering=-value")

        self.assertEqual(response.data["results"][0]["value"], "0.90000000")

    def test_the_page_size_is_bounded_by_the_paginator(self):
        for day in range(1, 31):
            self.add_snapshot(day=date(2024, 3, day))

        response = self.get_as(self.user, f"{self.url}?page_size=1000")

        self.assertEqual(response.data["count"], 30)
        self.assertLessEqual(len(response.data["results"]), 100)


class MetricsValidationTests(MetricsAPITestCase):
    def test_an_inverted_date_range_is_rejected(self):
        response = self.get_as(
            self.user, f"{self.url}?start_date=2024-03-15&end_date=2024-03-01"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("date_range", response.data["error"]["fields"])

    def test_a_date_that_is_not_iso_is_rejected(self):
        response = self.get_as(self.user, f"{self.url}?start_date=15/03/2024")

        self.assertEqual(response.status_code, 400)
        self.assertIn("date_range", response.data["error"]["fields"])

    def test_a_lookback_that_is_not_an_integer_is_rejected(self):
        response = self.get_as(self.user, f"{self.url}?lookback_days=thirty")

        self.assertEqual(response.status_code, 400)
        self.assertIn("lookback_days", response.data["error"]["fields"])

    def test_a_negative_lookback_is_rejected(self):
        response = self.get_as(self.user, f"{self.url}?lookback_days=-30")

        self.assertEqual(response.status_code, 400)
        self.assertIn("lookback_days", response.data["error"]["fields"])

    def test_an_unknown_instrument_is_a_404(self):
        response = self.get_as(self.user, "/api/v1/analytics/instruments/999999/metrics/")

        self.assertEqual(response.status_code, 404)

    def test_metrics_require_authentication(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
