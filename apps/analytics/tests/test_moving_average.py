"""Moving averages: what the window contains, and when it is not there yet.

The arithmetic on a hand-computed series, window 3, closes 10, 12, 11, 13, 15:

===========  -------  ----------------------------  ------------
trading day  close    window                        mean
===========  -------  ----------------------------  ------------
2024-03-11   10       one observation               undefined
2024-03-12   12       10, 12                        undefined
2024-03-13   11       10, 12, 11 -> 33 / 3         11
2024-03-14   13       12, 11, 13 -> 36 / 3         12
2024-03-15   15       11, 13, 15 -> 39 / 3         13
===========  -------  ----------------------------  ------------

On the standard sample series with ``start_date`` at DAY3 and window 3, the
warm-up makes every requested date answerable:

===========  -------  ----------------------------  ------------------------
trading day  close    window                        mean
===========  -------  ----------------------------  ------------------------
2024-03-13   101      100, 102, 101 -> 303 / 3     101.00000000
2024-03-14   103      102, 101, 103 -> 306 / 3     102.00000000
2024-03-15   104      101, 103, 104 -> 308 / 3     102.66666667
===========  -------  ----------------------------  ------------------------
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
    MovingAveragePoint,
    format_decimal,
    moving_average,
    quantize_price,
)
from ..series import load_price_series

DATES = [DAY1, DAY2, DAY3, DAY4, DAY5]
CLOSES = [Decimal(value) for value in ("10", "12", "11", "13", "15")]

AAPL_WINDOW_3_FROM_DAY3 = ["101.00000000", "102.00000000", "102.66666667"]


def values(points):
    return [format_decimal(point.value) for point in points]


def naive_series(dates, closes, window):
    """A fresh sum per window -- what the running sum must equal exactly."""
    out = []
    for index in range(len(closes)):
        sample = closes[max(0, index - window + 1) : index + 1]
        value = (
            None
            if len(sample) < window
            else quantize_price(sum((Decimal(c) for c in sample), Decimal(0)) / window)
        )
        out.append((dates[index], value))
    return out


class MovingAverageUnitTests(SimpleTestCase):
    def test_the_hand_computed_series(self):
        points = moving_average(DATES, CLOSES, 3)

        self.assertEqual(values(points), [None, None, "11.00000000", "12.00000000", "13.00000000"])

    def test_an_incomplete_window_is_undefined_rather_than_partial(self):
        points = moving_average(DATES, CLOSES, 7)

        self.assertEqual(values(points), [None] * 5)
        self.assertEqual([point.sample_size for point in points], [1, 2, 3, 4, 5])

    def test_sample_size_counts_the_observations_the_window_holds(self):
        points = moving_average(DATES, CLOSES, 3)

        self.assertEqual([point.sample_size for point in points], [1, 2, 3, 3, 3])
        self.assertEqual({point.window for point in points}, {3})

    def test_the_last_of_a_series_exactly_as_long_as_the_window_is_the_only_value(self):
        points = moving_average(DATES[:3], CLOSES[:3], 3)

        self.assertEqual(values(points), [None, None, "11.00000000"])

    def test_a_window_of_one_is_the_price_itself(self):
        points = moving_average(DATES, CLOSES, 1)

        self.assertEqual(values(points), ["10.00000000", "12.00000000", "11.00000000", "13.00000000", "15.00000000"])

    def test_a_mean_that_needs_more_than_eight_places_is_rounded_half_up(self):
        # 5 / 3 = 1.66666...6 -> 1.66666667
        points = moving_average(DATES[:3], [Decimal("1"), Decimal("2"), Decimal("2")], 3)

        self.assertEqual(values(points), [None, None, "1.66666667"])

    def test_the_running_sum_is_identical_to_a_fresh_sum(self):
        """The O(n) implementation is not an approximation.

        A rolling add/subtract is the classic place where float arithmetic
        drifts; in ``Decimal`` it cannot, and this asserts the identity on prices
        that have no exact binary representation.
        """
        closes = [Decimal(f"10.0000000{i}") for i in range(1, 10)]
        dates = [DAY1 + timedelta(days=index) for index in range(len(closes))]

        points = moving_average(dates, closes, 4)

        self.assertEqual(
            [(point.trading_date, point.value) for point in points],
            naive_series(dates, closes, 4),
        )

    def test_supplied_values_are_used_and_still_obey_the_completeness_rule(self):
        """The database's frame averages however many rows it has, so a supplied
        mean for a window that is not full is discarded in favour of ``None``."""
        supplied = [Decimal("999"), Decimal("999"), Decimal("11"), Decimal("12"), Decimal("13")]

        points = moving_average(DATES, CLOSES, 3, values=supplied)

        self.assertEqual(values(points), [None, None, "11.00000000", "12.00000000", "13.00000000"])

    def test_supplied_values_that_are_the_true_means_agree_with_the_computed_ones(self):
        computed = moving_average(DATES, CLOSES, 3)
        supplied = [point.value for point in computed]

        self.assertEqual(
            moving_average(DATES, CLOSES, 3, values=supplied),
            computed,
        )

    def test_a_window_below_one_is_rejected(self):
        with self.assertRaises(ValueError):
            moving_average(DATES, CLOSES, 0)
        with self.assertRaises(ValueError):
            moving_average(DATES, CLOSES, -3)

    def test_mismatched_lengths_are_rejected(self):
        with self.assertRaises(ValueError):
            moving_average(DATES, CLOSES[:2], 3)
        with self.assertRaises(ValueError):
            moving_average(DATES, CLOSES, 3, values=[Decimal("1")])

    def test_an_empty_series_is_an_empty_answer(self):
        self.assertEqual(moving_average([], [], 3), [])

    def test_points_compare_by_value(self):
        points = moving_average([DAY3], [Decimal("101")], 1)

        self.assertEqual(
            points[0],
            MovingAveragePoint(
                trading_date=DAY3,
                close=Decimal("101"),
                value=Decimal("101.00000000"),
                window=1,
                sample_size=1,
            ),
        )


class MovingAverageBackendAgreementTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()

    def test_the_three_backends_agree_on_the_sample_series(self):
        for symbol in ("AAPL", "MSFT", "GOOG"):
            with self.subTest(symbol=symbol):
                instrument = self.instruments[symbol]

                sql = services.moving_average_for_instrument(
                    instrument, window=3, start_date=DAY3, backend="sql"
                )
                python = services.moving_average_for_instrument(
                    instrument, window=3, start_date=DAY3, backend="python"
                )
                pandas = services.moving_average_for_instrument(
                    instrument, window=3, start_date=DAY3, backend="pandas"
                )

                self.assertEqual(sql.points, python.points)
                self.assertEqual(sql.points, pandas.points)

    def test_the_backends_agree_when_the_history_is_shorter_than_the_window(self):
        """The database's ``ROWS BETWEEN`` frame averages what it has; every
        backend must still decline to report a partial window."""
        for backend in services.MOVING_AVERAGE_BACKENDS:
            with self.subTest(backend=backend):
                result = services.moving_average_for_instrument(
                    self.instruments["AAPL"], window=7, backend=backend
                )

                self.assertEqual(values(result.points), [None] * 5)
                self.assertEqual([point.sample_size for point in result.points], [1, 2, 3, 4, 5])


class MovingAverageServiceTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()
        self.aapl = self.instruments["AAPL"]

    def test_the_warm_up_makes_the_first_requested_date_answerable(self):
        result = services.moving_average_for_instrument(
            self.aapl, window=3, start_date=DAY3
        )

        self.assertEqual(result.window, 3)
        self.assertEqual(result.padding, 2)
        self.assertEqual([point.trading_date for point in result.points], [DAY3, DAY4, DAY5])
        self.assertEqual(values(result.points), AAPL_WINDOW_3_FROM_DAY3)
        self.assertEqual(result.undefined, 0)

    def test_reading_the_same_dates_without_the_warm_up_leaves_holes(self):
        """The contrast that shows what ``pad`` buys, measured directly: the same
        arithmetic over a series that starts at DAY3 has nothing to average at
        first."""
        series = load_price_series(self.aapl, start_date=DAY3, end_date=DAY5)
        points = moving_average(series.dates, series.closes, 3)

        self.assertEqual(values(points), [None, None, "102.66666667"])

    def test_a_window_longer_than_the_history_reports_holes_not_a_short_mean(self):
        result = services.moving_average_for_instrument(self.aapl, window=7)

        self.assertEqual(result.undefined, 5)
        self.assertEqual([point.sample_size for point in result.points], [1, 2, 3, 4, 5])

    def test_the_window_counts_observations_not_calendar_days(self):
        """Prices on DAY1, DAY2 and DAY5 -- a four-day hole -- with a window of 3:
        the mean is of the three observations, so it exists on DAY5 even though
        three calendar days did not pass."""
        gapped = create_instrument("GAPPED")
        for day, close in ((DAY1, "10"), (DAY2, "20"), (DAY5, "60")):
            add_price(gapped, day, close, close, close, close, 1)

        result = services.moving_average_for_instrument(gapped, window=3)

        self.assertEqual([point.trading_date for point in result.points], [DAY1, DAY2, DAY5])
        self.assertEqual(values(result.points), [None, None, "30.00000000"])
        self.assertEqual((DAY5 - DAY1).days, 4)

    def test_the_whole_request_is_one_query(self):
        with self.assertNumQueries(1):
            services.moving_average_for_instrument(self.aapl, window=3, start_date=DAY3)

    def test_an_instrument_without_prices_returns_no_points(self):
        empty = create_instrument("EMPTY")

        result = services.moving_average_for_instrument(empty, window=3)

        self.assertEqual(result.points, [])
        self.assertEqual(result.undefined, 0)

    def test_a_window_below_one_is_rejected(self):
        with self.assertRaises(ValueError):
            services.moving_average_for_instrument(self.aapl, window=0)

    def test_an_unknown_backend_is_rejected_by_name(self):
        with self.assertRaises(ValueError) as caught:
            services.moving_average_for_instrument(self.aapl, window=3, backend="prophet")

        self.assertIn("prophet", str(caught.exception))
        self.assertIn("sql", str(caught.exception))
