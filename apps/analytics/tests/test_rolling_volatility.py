"""Rolling volatility: the deviations, the divisor, and the annualization.

Three hand-computed cases, each isolating one convention:

* **returns 1%, 2%, 3%** -- mean 2%, deviations -1%, 0, +1%, so the sum of squares
  is ``2e-4``, the sample variance is ``2e-4 / 2 = 1e-4`` and sigma is exactly
  ``0.01``. Annualized: ``0.01 x sqrt(252) = 0.15874508``.
* **returns +10%, -10%** -- mean 0, deviations +-10%, sum of squares ``0.02``, so
  the sample variance is ``0.02 / 1`` and sigma is ``0.1 x sqrt(2) = 0.14142136``.
  Dividing by ``window`` instead of ``window - 1`` would give ``0.1`` -- 41.4%
  smaller. This is the ``ddof`` case.
* **returns 10%, 10%, 10%** -- no deviation, so sigma is ``0`` exactly. Zero is a
  value, not a missing one.

On the standard sample series (AAPL) with window 3, from DAY4:

===========  ------  ---------------------  ------------  ------------
trading day  close   returns in the window  mean          annualized
===========  ------  ---------------------  ------------  ------------
2024-03-14   103     2.000%, -0.980%, 1.980% 0.999935%   0.27225453
2024-03-15   104     -0.980%, 1.980%, 0.971% 0.656893%   0.23892116
===========  ------  ---------------------  ------------  ------------
"""

from datetime import timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from apps.marketdata.tests.helpers import (
    DAY1,
    DAY2,
    DAY3,
    DAY4,
    DAY5,
    add_price,
    create_instrument,
    load_sample_series,
)

from .. import services
from ..metrics import (
    TRADING_DAYS_PER_YEAR,
    daily_returns,
    format_decimal,
    quantize_ratio,
    rolling_volatility,
)
from ..series import load_price_series  # noqa: F401

SAMPLE_DATES = [DAY1, DAY2, DAY3, DAY4, DAY5]

#: 0.1 * sqrt(2) and 0.1 * sqrt(504) = 0.1 * sqrt(2) * sqrt(252): the annualized
#: figure for a window whose two returns are +10% and -10%.
SIGMA_OF_PLUS_MINUS_TEN = "0.14142136"
ANNUALIZED_OF_PLUS_MINUS_TEN = "2.24499443"


def values(points):
    return [format_decimal(point.value) for point in points]


def dailies(points):
    return [format_decimal(point.daily) for point in points]


def means(points):
    return [format_decimal(point.mean) for point in points]


class VolatilityUnitTests(SimpleTestCase):
    def test_a_hand_computed_window(self):
        returns = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]

        points = rolling_volatility(SAMPLE_DATES[:3], returns, 3)

        self.assertEqual(values(points), [None, None, "0.15874508"])
        self.assertEqual(dailies(points), [None, None, "0.01000000"])
        self.assertEqual(means(points), [None, None, "0.02000000"])

    def test_the_divisor_is_window_minus_one_not_window(self):
        """``ddof=1``: the returns are a sample of the process, not the
        population, and here the two answers differ by 41.4%."""
        returns = [Decimal("0.10"), Decimal("-0.10")]

        points = rolling_volatility(SAMPLE_DATES[:2], returns, 2)

        self.assertEqual(dailies(points), [None, SIGMA_OF_PLUS_MINUS_TEN])
        self.assertEqual(values(points), [None, ANNUALIZED_OF_PLUS_MINUS_TEN])
        # The population sigma would be 0.1, i.e. the sample sigma / sqrt(2).
        self.assertAlmostEqual(
            float(Decimal(SIGMA_OF_PLUS_MINUS_TEN) / Decimal("0.1")),
            float(Decimal(2).sqrt()),
            places=6,
        )

    def test_a_flat_window_has_zero_volatility_which_is_a_value(self):
        returns = [Decimal("0.10")] * 3

        points = rolling_volatility(SAMPLE_DATES[:3], returns, 3)

        self.assertEqual(dailies(points)[-1], "0.00000000")
        self.assertEqual(values(points)[-1], "0.00000000")
        self.assertEqual(means(points)[-1], "0.10000000")

    def test_the_annualized_figure_is_the_daily_figure_times_root_252(self):
        returns = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]
        points = rolling_volatility(SAMPLE_DATES[:3], returns, 3)
        point = points[-1]

        self.assertEqual(TRADING_DAYS_PER_YEAR, 252)
        # The two figures are rounded independently, so they agree to within the
        # quantum carried through the annualizer rather than exactly.
        self.assertLessEqual(
            abs(point.value - point.daily * Decimal(TRADING_DAYS_PER_YEAR).sqrt()),
            Decimal("0.0000002"),
        )

    def test_the_annualization_factor_is_a_parameter_not_a_constant(self):
        returns = [Decimal("0.10"), Decimal("-0.10")]

        points = rolling_volatility(SAMPLE_DATES[:2], returns, 2, periods_per_year=52)

        # 0.1 * sqrt(2) * sqrt(52) = 0.1 * sqrt(104)
        self.assertEqual(values(points)[-1], "1.01980390")

    def test_an_undefined_return_makes_every_window_that_contains_it_undefined(self):
        returns = [Decimal("0.01"), None, Decimal("0.03")]

        points = rolling_volatility(SAMPLE_DATES[:3], returns, 2)

        self.assertEqual(values(points), [None, None, None])
        self.assertEqual([point.sample_size for point in points], [1, 1, 1])

    def test_too_little_history_is_undefined_not_a_short_window(self):
        returns = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]

        points = rolling_volatility(SAMPLE_DATES[:3], returns, 3)

        self.assertEqual(points[0].sample_size, 1)
        self.assertEqual(points[1].sample_size, 2)
        self.assertEqual(points[2].sample_size, 3)
        self.assertEqual({point.window for point in points}, {3})

    def test_a_window_of_one_return_is_rejected(self):
        """A sample standard deviation of one observation is a division by zero."""
        with self.assertRaises(ValueError):
            rolling_volatility(SAMPLE_DATES[:2], [Decimal("0.01")], 1)
        with self.assertRaises(ValueError):
            rolling_volatility(SAMPLE_DATES[:2], [Decimal("0.01"), Decimal("0.02")], 0)

    def test_mismatched_lengths_are_rejected(self):
        with self.assertRaises(ValueError):
            rolling_volatility(SAMPLE_DATES, [Decimal("0.01")], 2)

    def test_an_empty_series_is_an_empty_answer(self):
        self.assertEqual(rolling_volatility([], [], 2), [])

    def test_the_reported_returns_reproduce_the_volatility(self):
        """Reproducibility: a window's sigma can be recomputed from the daily
        returns the API reports, with the definition written out here rather than
        called."""
        closes = [Decimal(value) for value in ("100", "102", "101", "103", "104")]
        reported = [point.value for point in daily_returns(SAMPLE_DATES, closes)]
        window = reported[2:5]  # the three returns ending at DAY5

        mean = sum(window, Decimal(0)) / 3
        variance = sum(((value - mean) ** 2 for value in window), Decimal(0)) / 2
        annualized = variance.sqrt() * Decimal(252).sqrt()

        self.assertEqual(format_decimal(quantize_ratio(annualized)), "0.23892116")
        self.assertEqual(
            format_decimal(rolling_volatility(SAMPLE_DATES, reported, 3)[-1].value),
            "0.23892116",
        )


class VolatilityBackendAgreementTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()

    def test_the_backends_agree_point_for_point(self):
        for symbol in ("AAPL", "MSFT", "GOOG"):
            for window in (2, 3):
                with self.subTest(symbol=symbol, window=window):
                    instrument = self.instruments[symbol]

                    reference = services.rolling_volatility_for_instrument(
                        instrument, window=window, backend="python"
                    )
                    sql = services.rolling_volatility_for_instrument(
                        instrument, window=window, backend="sql"
                    )
                    pandas = services.rolling_volatility_for_instrument(
                        instrument, window=window, backend="pandas"
                    )

                    self.assertEqual(reference.points, sql.points)
                    self.assertEqual(reference.points, pandas.points)

    def test_the_backends_agree_across_the_warm_up_boundary(self):
        """The date at the edge of the warm-up is where an off-by-one in ``pad``
        would show, so it is asserted explicitly."""
        instrument = self.instruments["AAPL"]

        result = services.rolling_volatility_for_instrument(
            instrument, window=3, start_date=DAY4, backend="sql"
        )

        self.assertEqual([point.trading_date for point in result.points], [DAY4, DAY5])
        self.assertEqual(result.padding, 3)
        self.assertEqual(values(result.points), ["0.27225453", "0.23892116"])
        self.assertEqual(dailies(result.points), ["0.01715042", "0.01505062"])
        self.assertEqual(result.undefined, 0)


class VolatilityServiceTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()
        self.aapl = self.instruments["AAPL"]

    def test_the_window_is_reported_back_with_the_answer(self):
        result = services.rolling_volatility_for_instrument(self.aapl, window=3)

        self.assertEqual(result.window, 3)
        self.assertEqual(result.periods_per_year, TRADING_DAYS_PER_YEAR)

    def test_the_whole_request_is_one_query(self):
        with self.assertNumQueries(1):
            services.rolling_volatility_for_instrument(
                self.aapl, window=3, start_date=DAY4
            )

    def test_a_young_series_reports_holes(self):
        result = services.rolling_volatility_for_instrument(self.aapl, window=3)

        # Returns exist from DAY2, and the first of them has no predecessor, so a
        # window of three returns first completes on DAY4: DAY1 to DAY3 are holes.
        self.assertEqual(result.undefined, 3)
        self.assertEqual(
            [point.trading_date for point in result.points], [DAY1, DAY2, DAY3, DAY4, DAY5]
        )
        self.assertIsNone(result.points[2].value)
        self.assertEqual(format_decimal(result.points[3].value), "0.27225453")

    def test_a_zero_close_leaves_the_windows_that_contain_its_return_undefined(self):
        zeroed = create_instrument("ZEROED")
        for offset, close in enumerate(["10", "0", "5"]):
            add_price(
                zeroed,
                DAY1 + timedelta(days=offset),
                close,
                close,
                close,
                close,
                1,
            )

        result = services.rolling_volatility_for_instrument(zeroed, window=2)

        # Returns: -100% (from 10 to 0), then undefined (0 to 5).
        self.assertEqual(values(result.points), [None, None, None])
        self.assertEqual(result.undefined, 3)

    def test_a_flat_price_series_has_zero_volatility_on_every_covered_date(self):
        flat = create_instrument("FLAT")
        for offset in range(5):
            add_price(
                flat,
                DAY1 + timedelta(days=offset),
                "50",
                "50",
                "50",
                "50",
                1,
            )

        result = services.rolling_volatility_for_instrument(flat, window=2)

        # Day one has one return (which itself has no predecessor), so only the
        # three dates whose window is two defined returns carry a value.
        self.assertEqual(result.undefined, 2)
        self.assertEqual(values(result.points)[2:], ["0.00000000"] * 3)

    def test_an_instrument_without_prices_returns_no_points(self):
        empty = create_instrument("EMPTY")

        result = services.rolling_volatility_for_instrument(empty, window=3)

        self.assertEqual(result.points, [])

    def test_a_window_of_one_return_is_rejected(self):
        with self.assertRaises(ValueError):
            services.rolling_volatility_for_instrument(self.aapl, window=1)

    def test_an_unknown_backend_is_rejected_by_name(self):
        with self.assertRaises(ValueError) as caught:
            services.rolling_volatility_for_instrument(
                self.aapl, window=3, backend="prophet"
            )

        self.assertIn("prophet", str(caught.exception))
        self.assertIn("sql", str(caught.exception))
