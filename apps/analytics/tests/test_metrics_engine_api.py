"""The HTTP surface of the metrics engine.

These are *adapter* tests. The arithmetic is tested where it lives -- one file per
metric -- so what is asserted here is only what the HTTP layer adds: that a request
parses into the right service call, that a default is the engine's default rather
than a second one invented at the edge, and that a bad request is a 400 rather than
a 500.

The strongest test in the file is the round trip: for every metric, the rendered
body equals the serializer's rendering of the service call made directly. That is
what makes "the endpoint is the engine" a fact rather than a claim -- a second
implementation hidden in the view could not pass it.
"""

from urllib.parse import urlencode

from apps.analytics import services
from apps.analytics.metrics import MOVING_AVERAGE_NAMES
from apps.analytics.serializers import (
    CumulativeReturnsSerializer,
    DailyReturnsSerializer,
    MaximumDrawdownSerializer,
    MovingAveragesSerializer,
    PercentageChangeSerializer,
    PriceExtremesSerializer,
    RankingSerializer,
    RollingVolatilitySerializer,
    VolumeSummariesSerializer,
)
from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.marketdata.tests.helpers import (
    DAY1,
    DAY2,
    DAY4,
    DAY5,
    load_sample_series,
)


class EngineAPITestCase(AuthenticatedAPITestCase):
    """The standard three-instrument sample, reachable through the two routes."""

    def setUp(self):
        super().setUp()
        self.instruments = load_sample_series()
        self.aapl = self.instruments["AAPL"]
        self.series_url = f"/api/v1/analytics/instruments/{self.aapl.pk}/series/"
        self.ranking_url = "/api/v1/analytics/rankings/"

    def get_series(self, **params):
        return self.get_as(self.user, f"{self.series_url}?{urlencode(params)}")

    def get_ranking(self, **params):
        return self.get_as(self.user, f"{self.ranking_url}?{urlencode(params)}")

    def places(self, response):
        """The (rank, symbol, value) triples of a ranking body."""
        return [
            (row["rank"], row["symbol"], row["value"]) for row in response.data["rows"]
        ]


class ComputationLoggingTests(EngineAPITestCase):
    """A computation leaves a line saying what ran and how long it took.

    These are the lines that answer "why was that request slow?" -- the request
    log says the endpoint took 1.4s, and this says which metric ran, on which
    backend, and for how long.
    """

    def test_a_series_computation_is_logged_with_its_backend_and_duration(self):
        with self.assertLogs("market_data.analytics", level="INFO") as captured:
            response = self.get_series(metric="returns")

        self.assertEqual(response.status_code, 200)
        message = captured.records[0].getMessage()
        self.assertIn("metric=returns", message)
        self.assertIn(f"instrument={self.aapl.pk}", message)
        self.assertIn("backend=", message)
        self.assertIn("ms", message)

    def test_a_ranking_computation_is_logged(self):
        with self.assertLogs("market_data.analytics", level="INFO") as captured:
            response = self.get_ranking(measure="return")

        self.assertEqual(response.status_code, 200)
        message = captured.records[0].getMessage()
        self.assertIn("ranking", message)
        self.assertIn("measure=return", message)

    def test_a_rejected_metric_logs_nothing(self):
        # A 400 never reaches the engine, so the computation logger stays quiet.
        with self.assertNoLogs("market_data.analytics", level="INFO"):
            response = self.get_series(metric="not_a_metric")

        self.assertEqual(response.status_code, 400)


class DefaultsTests(EngineAPITestCase):
    """What a bare request asks for, without the client naming anything."""

    def test_the_default_metric_is_daily_returns_over_the_whole_history(self):
        response = self.get_series()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["symbol"], "AAPL")
        # No bound was given, so nothing is dropped as warm-up: the whole stored
        # history is answered, and its first session has no predecessor to return
        # from -- a fact about the data, reported as null and counted.
        self.assertEqual(response.data["padding"], 0)
        self.assertEqual(response.data["undefined"], 1)
        self.assertEqual(
            [point["trading_date"] for point in response.data["points"]],
            ["2024-03-11", "2024-03-12", "2024-03-13", "2024-03-14", "2024-03-15"],
        )
        self.assertIsNone(response.data["points"][0]["value"])
        self.assertIsNone(response.data["start_date"])
        self.assertIsNone(response.data["end_date"])

    def test_the_default_backend_is_the_one_that_exists_for_the_metric(self):
        # A metric with a SQL implementation defaults to it; one without defaults
        # to Python. The endpoint does not pick a backend of its own.
        self.assertEqual(self.get_series().data["backend"], "sql")
        self.assertEqual(
            self.get_series(metric="percentage_change").data["backend"], "python"
        )
        self.assertEqual(
            self.get_series(metric="cumulative_returns").data["backend"], "python"
        )

    def test_the_default_volatility_window_is_thirty_sessions(self):
        self.assertEqual(self.get_series(metric="volatility").data["window"], 30)

    def test_the_default_moving_averages_are_all_three(self):
        response = self.get_series(metric="moving_averages")

        self.assertEqual(response.data["windows"], [7, 30, 90])
        self.assertEqual(
            [named["name"] for named in response.data["series"]],
            list(MOVING_AVERAGE_NAMES),
        )

    def test_the_default_ranking_is_by_return_best_first_over_everything(self):
        response = self.get_ranking()

        self.assertEqual(response.data["measure"], "return")
        self.assertIs(response.data["descending"], True)
        self.assertEqual(response.data["universe"], 3)
        self.assertEqual(response.data["truncated"], 0)
        self.assertIsNone(response.data["lookback"])

    def test_an_empty_parameter_is_the_same_as_an_omitted_one(self):
        self.assertEqual(self.get_series(metric="").data["backend"], "sql")


class HandCheckedValuesTests(EngineAPITestCase):
    """The numbers, against the arithmetic done by hand in the docs.

    AAPL closes 100, 102, 101, 103, 104 on five consecutive sessions.
    """

    def test_daily_returns_are_the_session_over_session_ratios(self):
        # Bounded at DAY2 so the session before it is read as warm-up: the answer
        # is four returns from a clean base, with the warm-up row accounted for by
        # ``padding`` rather than reported as if it were an answer.
        response = self.get_series(metric="returns", start_date=DAY2.isoformat())

        self.assertEqual(response.data["padding"], 1)
        self.assertEqual(
            [point["value"] for point in response.data["points"]],
            # (102-100)/100, (101-102)/102, (103-101)/101, (104-103)/103
            ["0.02000000", "-0.00980392", "0.01980198", "0.00970874"],
        )

    def test_cumulative_returns_grow_from_the_first_close_of_the_range(self):
        response = self.get_series(metric="cumulative_returns")

        self.assertEqual(response.data["total"], "0.04000000")
        self.assertEqual(
            [point["value"] for point in response.data["points"]],
            [
                "0.00000000",
                "0.02000000",
                "0.01000000",
                "0.03000000",
                "0.04000000",
            ],
        )

    def test_the_percentage_change_is_the_ratio_in_percent(self):
        period = self.get_series(metric="percentage_change").data["periods"][0]

        self.assertEqual(period["base_date"], "2024-03-11")
        self.assertEqual(period["base_close"], "100.00000000")
        self.assertEqual(period["end_date"], "2024-03-15")
        self.assertEqual(period["end_close"], "104.00000000")
        self.assertEqual(period["change"], "0.04000000")
        self.assertEqual(period["percent_change"], "4.00000000")
        self.assertEqual(period["observations"], 5)

    def test_the_extremes_are_the_highest_and_lowest_of_the_range(self):
        summary = self.get_series(metric="extremes").data["summary"]

        self.assertEqual(summary["highest_close"], "104.00000000")
        self.assertEqual(summary["highest_close_date"], "2024-03-15")
        self.assertEqual(summary["lowest_close"], "100.00000000")
        self.assertEqual(summary["lowest_close_date"], "2024-03-11")
        self.assertEqual(summary["traded_range"], "4.00000000")

    def test_the_volume_summary_totals_the_sessions(self):
        summary = self.get_series(metric="volume").data["summary"]

        self.assertEqual(summary["total_volume"], "5000")
        self.assertEqual(summary["average_volume"], "1000")
        self.assertEqual(summary["observations"], 5)
        self.assertEqual(summary["sessions_at_average"], 5)

    def test_the_return_ranking_is_the_hand_computed_order(self):
        response = self.get_ranking(measure="return")

        self.assertEqual(
            self.places(response),
            [
                (1, "GOOG", "0.08000000"),
                (2, "MSFT", "0.05000000"),
                (3, "AAPL", "0.04000000"),
            ],
        )
        self.assertEqual(response.data["ranked"], 3)
        self.assertEqual(response.data["unranked"], 0)
        self.assertEqual(response.data["truncated"], 0)

    def test_the_volume_ranking_is_the_hand_computed_order(self):
        response = self.get_ranking(measure="volume")

        self.assertEqual(
            self.places(response),
            [
                (1, "GOOG", "10000"),
                (2, "AAPL", "5000"),
                (3, "MSFT", "2500"),
            ],
        )


class RoundTripTests(EngineAPITestCase):
    """The endpoint is the engine: the body equals the service call's rendering."""

    def assert_round_trip(self, metric, service_call, serializer, **params):
        response = self.get_series(metric=metric, **params)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, serializer(service_call()).data)

    def test_returns(self):
        self.assert_round_trip(
            "returns",
            lambda: services.daily_returns_for_instrument(self.aapl),
            DailyReturnsSerializer,
        )

    def test_cumulative_returns(self):
        self.assert_round_trip(
            "cumulative_returns",
            lambda: services.cumulative_returns_for_instrument(self.aapl),
            CumulativeReturnsSerializer,
        )

    def test_moving_averages(self):
        self.assert_round_trip(
            "moving_averages",
            lambda: services.moving_averages_for_instrument(self.aapl),
            MovingAveragesSerializer,
        )

    def test_volatility(self):
        self.assert_round_trip(
            "volatility",
            lambda: services.rolling_volatility_for_instrument(self.aapl, window=30),
            RollingVolatilitySerializer,
        )

    def test_drawdown(self):
        self.assert_round_trip(
            "drawdown",
            lambda: services.maximum_drawdown_for_instrument(self.aapl),
            MaximumDrawdownSerializer,
        )

    def test_extremes(self):
        self.assert_round_trip(
            "extremes",
            lambda: services.price_extremes_for_instrument(self.aapl),
            PriceExtremesSerializer,
        )

    def test_volume(self):
        self.assert_round_trip(
            "volume",
            lambda: services.volume_summaries_for_instrument(self.aapl),
            VolumeSummariesSerializer,
        )

    def test_percentage_change(self):
        self.assert_round_trip(
            "percentage_change",
            lambda: services.percentage_change_for_instrument(self.aapl),
            PercentageChangeSerializer,
        )

    def test_a_bounded_range_is_the_range_the_service_was_asked_for(self):
        self.assert_round_trip(
            "returns",
            lambda: services.daily_returns_for_instrument(
                self.aapl, start_date=DAY2, end_date=DAY4
            ),
            DailyReturnsSerializer,
            start_date=DAY2.isoformat(),
            end_date=DAY4.isoformat(),
        )

    def test_the_ranking(self):
        response = self.get_ranking(measure="volume", lookback=2)
        expected = RankingSerializer(
            services.ranking_for_instruments(measure="volume", lookback=2)
        ).data

        self.assertEqual(response.data, expected)

    def test_the_body_renders_as_json(self):
        # A Decimal or a date left unconverted would fail here rather than in a
        # client: the fields are strings and ISO dates by construction.
        response = self.get_series(metric="drawdown")

        self.assertEqual(response.status_code, 200)
        self.assertIn("points", response.json())


class RangeAndBackendTests(EngineAPITestCase):
    def test_the_date_bounds_are_inclusive(self):
        response = self.get_series(
            metric="returns", start_date=DAY2.isoformat(), end_date=DAY4.isoformat()
        )

        self.assertEqual(
            [point["trading_date"] for point in response.data["points"]],
            ["2024-03-12", "2024-03-13", "2024-03-14"],
        )

    def test_a_backend_may_be_chosen_when_the_metric_has_it(self):
        bounded = {"metric": "returns", "start_date": DAY2.isoformat()}
        expected = [
            point["value"]
            for point in self.get_series(backend="sql", **bounded).data["points"]
        ]

        for backend in ("python", "pandas"):
            with self.subTest(backend=backend):
                response = self.get_series(backend=backend, **bounded)
                self.assertEqual(response.data["backend"], backend)
                self.assertEqual(
                    [point["value"] for point in response.data["points"]], expected
                )

    def test_a_backend_the_metric_does_not_have_is_rejected(self):
        # There is no SQL cumulative return -- the chain break has no window form.
        response = self.get_series(metric="cumulative_returns", backend="sql")

        self.assertEqual(response.status_code, 400)
        self.assertIn("metric", response.data["error"]["fields"])

    def test_an_unknown_backend_is_rejected(self):
        response = self.get_series(metric="returns", backend="numpy")

        self.assertEqual(response.status_code, 400)
        self.assertIn("metric", response.data["error"]["fields"])

    def test_an_inverted_range_is_rejected(self):
        response = self.get_series(
            metric="returns", start_date=DAY4.isoformat(), end_date=DAY1.isoformat()
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("metric", response.data["error"]["fields"])


class MetricParameterTests(EngineAPITestCase):
    def test_the_volatility_window_is_the_window_asked_for(self):
        response = self.get_series(metric="volatility", window=2)

        self.assertEqual(response.data["window"], 2)
        # Five sessions, but only the last three have two returns behind them: the
        # first has none and the second has one, so both are null and counted.
        self.assertEqual(len(response.data["points"]), 5)
        self.assertEqual(response.data["undefined"], 2)

    def test_a_window_below_two_is_rejected(self):
        response = self.get_series(metric="volatility", window=1)

        self.assertEqual(response.status_code, 400)
        self.assertIn("window", response.data["error"]["fields"])

    def test_a_window_that_is_not_an_integer_is_rejected(self):
        response = self.get_series(metric="volatility", window="thirty")

        self.assertEqual(response.status_code, 400)
        self.assertIn("window", response.data["error"]["fields"])

    def test_the_moving_averages_can_be_narrowed_to_one_series(self):
        response = self.get_series(metric="moving_averages", names="moving_average_7")

        self.assertEqual(response.data["windows"], [7])
        self.assertEqual([named["name"] for named in response.data["series"]], [
            "moving_average_7"
        ])

    def test_the_moving_averages_can_be_a_subset(self):
        response = self.get_series(
            metric="moving_averages", names="moving_average_7,moving_average_90"
        )

        self.assertEqual(response.data["windows"], [7, 90])

    def test_an_unknown_moving_average_name_is_rejected(self):
        response = self.get_series(metric="moving_averages", names="moving_average_5")

        self.assertEqual(response.status_code, 400)
        self.assertIn("names", response.data["error"]["fields"])

    def test_several_periods_are_answered_from_one_request(self):
        response = self.get_series(
            metric="percentage_change",
            periods=(
                '[{"label": "first half", "start_date": "2024-03-11", '
                '"end_date": "2024-03-13"}, {"label": "second half", '
                '"start_date": "2024-03-13", "end_date": "2024-03-15"}]'
            ),
        )

        self.assertEqual(
            [
                (period["label"], period["change"], period["observations"])
                for period in response.data["periods"]
            ],
            [
                ("first half", "0.01000000", 3),
                ("second half", "0.02970297", 3),
            ],
        )

    def test_a_period_may_omit_its_label(self):
        response = self.get_series(
            metric="percentage_change",
            periods='[{"start_date": "2024-03-11", "end_date": "2024-03-15"}]',
        )

        self.assertIsNone(response.data["periods"][0]["label"])

    def test_periods_and_the_date_bounds_cannot_both_be_given(self):
        response = self.get_series(
            metric="percentage_change",
            start_date=DAY1.isoformat(),
            periods='[{"start_date": "2024-03-11", "end_date": "2024-03-15"}]',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("metric", response.data["error"]["fields"])

    def test_periods_that_are_not_json_are_rejected(self):
        response = self.get_series(metric="percentage_change", periods="q1")

        self.assertEqual(response.status_code, 400)
        self.assertIn("periods", response.data["error"]["fields"])

    def test_periods_that_are_not_an_array_are_rejected(self):
        response = self.get_series(metric="percentage_change", periods='{"a": 1}')

        self.assertEqual(response.status_code, 400)
        self.assertIn("periods", response.data["error"]["fields"])

    def test_a_period_without_both_dates_is_rejected(self):
        response = self.get_series(
            metric="percentage_change", periods='[{"start_date": "2024-03-11"}]'
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("periods", response.data["error"]["fields"])


class RankingParameterTests(EngineAPITestCase):
    def test_the_window_is_the_newest_n_sessions_of_each_instrument(self):
        response = self.get_ranking(lookback=2)

        # Two sessions move MSFT to the top: 205 -> 210 is a bigger move than
        # GOOG's 53 -> 54.
        self.assertEqual(
            self.places(response),
            [
                (1, "MSFT", "0.02439024"),
                (2, "GOOG", "0.01886792"),
                (3, "AAPL", "0.00970874"),
            ],
        )
        for row in response.data["rows"]:
            self.assertEqual(row["observations"], 2)

    def test_ascending_reverses_the_ranking(self):
        response = self.get_ranking(descending="false")

        self.assertEqual(
            [symbol for _, symbol, _ in self.places(response)],
            ["AAPL", "MSFT", "GOOG"],
        )

    def test_a_limit_truncates_without_renumbering(self):
        response = self.get_ranking(limit=1)

        self.assertEqual(self.places(response), [(1, "GOOG", "0.08000000")])
        self.assertEqual(response.data["truncated"], 2)
        # The counts describe the universe, not the page.
        self.assertEqual(response.data["universe"], 3)
        self.assertEqual(response.data["ranked"], 3)

    def test_the_universe_can_be_named(self):
        response = self.get_ranking(instrument_ids=str(self.aapl.pk))

        self.assertEqual(response.data["universe"], 1)
        self.assertEqual(self.places(response), [(1, "AAPL", "0.04000000")])

    def test_the_date_bounds_restrict_the_window(self):
        response = self.get_ranking(
            start_date=DAY2.isoformat(), end_date=DAY5.isoformat()
        )

        # Measured from DAY2: MSFT 190 -> 210 is +10.5%, GOOG 51 -> 54 is +5.9%,
        # AAPL 102 -> 104 is +2.0%.
        self.assertEqual(
            self.places(response),
            [
                (1, "MSFT", "0.10526316"),
                (2, "GOOG", "0.05882353"),
                (3, "AAPL", "0.01960784"),
            ],
        )

    def test_an_unknown_measure_is_rejected(self):
        response = self.get_ranking(measure="turnover")

        self.assertEqual(response.status_code, 400)
        self.assertIn("measure", response.data["error"]["fields"])

    def test_a_lookback_below_one_is_rejected(self):
        response = self.get_ranking(lookback=0)

        self.assertEqual(response.status_code, 400)
        self.assertIn("lookback", response.data["error"]["fields"])

    def test_a_negative_limit_is_rejected(self):
        response = self.get_ranking(limit=-1)

        self.assertEqual(response.status_code, 400)
        self.assertIn("limit", response.data["error"]["fields"])

    def test_an_instrument_id_that_is_not_an_integer_is_rejected(self):
        response = self.get_ranking(instrument_ids="AAPL")

        self.assertEqual(response.status_code, 400)
        self.assertIn("instrument_ids", response.data["error"]["fields"])

    def test_an_unknown_backend_is_rejected(self):
        response = self.get_ranking(backend="sql")

        self.assertEqual(response.status_code, 400)
        self.assertIn("ranking", response.data["error"]["fields"])


class RequestShapeTests(EngineAPITestCase):
    def test_an_unknown_metric_is_rejected_with_the_vocabulary(self):
        response = self.get_series(metric="turnover")

        self.assertEqual(response.status_code, 400)
        fields = response.data["error"]["fields"]
        self.assertIn("metric", fields)
        self.assertIn("volume", fields["metric"][0])

    def test_a_parameter_the_metric_does_not_take_is_rejected(self):
        # Silently ignoring this would let a client believe a window applied.
        response = self.get_series(metric="moving_averages", window=7)

        self.assertEqual(response.status_code, 400)
        self.assertIn("query_params", response.data["error"]["fields"])

    def test_a_parameter_that_is_not_known_at_all_is_rejected(self):
        response = self.get_series(metric="returns", windows=7)

        self.assertEqual(response.status_code, 400)
        self.assertIn("query_params", response.data["error"]["fields"])

    def test_a_ranking_rejects_a_parameter_it_does_not_take(self):
        response = self.get_ranking(window=7)

        self.assertEqual(response.status_code, 400)
        self.assertIn("query_params", response.data["error"]["fields"])

    def test_format_is_accepted_because_drf_reads_it(self):
        self.assertEqual(self.get_series(format="json").status_code, 200)
        self.assertEqual(self.get_ranking(format="json").status_code, 200)

    def test_a_date_that_is_not_iso_is_rejected(self):
        response = self.get_series(metric="returns", start_date="15/03/2024")

        self.assertEqual(response.status_code, 400)
        self.assertIn("start_date", response.data["error"]["fields"])

    def test_an_unknown_instrument_is_a_404(self):
        response = self.get_as(
            self.user, "/api/v1/analytics/instruments/999999/series/"
        )

        self.assertEqual(response.status_code, 404)

    def test_the_series_requires_authentication(self):
        self.assertEqual(self.client.get(self.series_url).status_code, 401)

    def test_the_ranking_requires_authentication(self):
        self.assertEqual(self.client.get(self.ranking_url).status_code, 401)
