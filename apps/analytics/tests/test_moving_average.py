"""Moving averages: the three named series, what their window contains, and when
it is not there yet.

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

On the standard sample series with ``start_date`` at DAY3 and the 3-observation
window, the warm-up makes every requested date answerable:

===========  -------  ----------------------------  ------------------------
trading day  close    window                        mean
===========  -------  ----------------------------  ------------------------
2024-03-13   101      100, 102, 101 -> 303 / 3     101.00000000
2024-03-14   103      102, 101, 103 -> 306 / 3     102.00000000
2024-03-15   104      101, 103, 104 -> 308 / 3     102.66666667
===========  -------  ----------------------------  ------------------------

The conventional series are named rather than parameterized, so the service tests
use a twelve-session series with closes 10, 11, ... 21, where each window's trailing
mean is distinguishable:

===========  -------  -----------------------------  ------------------------
trading day  close    7-observation window           mean
===========  -------  -----------------------------  ------------------------
2024-03-17   16       rows 1..7, seven of them       13
2024-03-19   18       rows 3..9, seven of them       15
2024-03-22   21       rows 6..12, seven of them      18
===========  -------  -----------------------------  ------------------------
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
    MOVING_AVERAGE_NAMES,
    MovingAveragePoint,
    format_decimal,
    moving_average,
    moving_average_series,
    quantize_price,
)
from ..series import load_price_series

DATES = [DAY1, DAY2, DAY3, DAY4, DAY5]
CLOSES = [Decimal(value) for value in ("10", "12", "11", "13", "15")]

AAPL_WINDOW_3_FROM_DAY3 = ["101.00000000", "102.00000000", "102.66666667"]

#: Twelve consecutive sessions, 10 through 21, so every window has a different mean.
TWELVE_CLOSES = [Decimal(value) for value in range(10, 22)]


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


def build_series(symbol, closes):
    """An instrument carrying the given closes on consecutive sessions."""
    instrument = create_instrument(symbol)
    for offset, close in enumerate(closes):
        add_price(
            instrument,
            DAY1 + timedelta(days=offset),
            close,
            close,
            close,
            close,
            1,
        )
    return instrument


def by_name(result):
    return {named.name: values(named.points) for named in result.series}


class NamedSeriesTests(SimpleTestCase):
    """The vocabulary itself: three names, and nothing else."""

    def test_the_conventional_series_are_named_and_ordered(self):
        wanted = moving_average_series()

        self.assertEqual([series.name for series in wanted], list(MOVING_AVERAGE_NAMES))
        self.assertEqual([series.window for series in wanted], [7, 30, 90])

    def test_a_selection_is_answered_in_the_conventional_order(self):
        """Not the requested order: the three series have a canonical order, and a
        caller asking for 90 then 7 gets the same answer as one asking for 7 then 90."""
        wanted = moving_average_series(["moving_average_90", "moving_average_7"])

        self.assertEqual([series.window for series in wanted], [7, 90])

    def test_a_repeated_name_is_computed_once(self):
        wanted = moving_average_series(["moving_average_7", "moving_average_7"])

        self.assertEqual([series.window for series in wanted], [7])

    def test_an_empty_selection_is_an_empty_selection(self):
        self.assertEqual(moving_average_series([]), ())

    def test_an_unknown_name_is_rejected_with_the_vocabulary(self):
        with self.assertRaises(ValueError) as caught:
            moving_average_series(["moving_average_50"])

        self.assertIn("moving_average_50", str(caught.exception))
        self.assertIn("moving_average_7", str(caught.exception))


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
        self.twelve = build_series("TWELVE", TWELVE_CLOSES)

    def test_the_three_backends_agree_on_every_named_series(self):
        reference = services.moving_averages_for_instrument(
            self.twelve, backend="python"
        )

        for backend in services.MOVING_AVERAGE_BACKENDS:
            with self.subTest(backend=backend):
                result = services.moving_averages_for_instrument(
                    self.twelve, backend=backend
                )

                for left, right in zip(result.series, reference.series):
                    self.assertEqual(left.name, right.name)
                    self.assertEqual(left.points, right.points, backend)

    def test_the_backends_agree_when_the_history_is_shorter_than_the_window(self):
        """The database's ``ROWS BETWEEN`` frame averages what it has; every
        backend must still decline to report a partial window."""
        for backend in services.MOVING_AVERAGE_BACKENDS:
            with self.subTest(backend=backend):
                result = services.moving_averages_for_instrument(
                    self.instruments["AAPL"], backend=backend
                )

                for named in result.series:
                    self.assertEqual(values(named.points), [None] * 5, named.name)
                    self.assertEqual(
                        [point.sample_size for point in named.points], [1, 2, 3, 4, 5]
                    )


class MovingAverageServiceTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()
        self.aapl = self.instruments["AAPL"]
        self.twelve = build_series("TWELVE", TWELVE_CLOSES)

    def test_the_default_is_the_three_conventional_series_in_one_query(self):
        with self.assertNumQueries(1):
            result = services.moving_averages_for_instrument(self.twelve)

        self.assertEqual([named.name for named in result.series], list(MOVING_AVERAGE_NAMES))
        self.assertEqual(result.windows, (7, 30, 90))

    def test_the_seven_observation_window_ends_on_the_hand_computed_mean(self):
        result = services.moving_averages_for_instrument(
            self.twelve, names=("moving_average_7",)
        )
        points = result.named("moving_average_7").points

        # 12 rows, closes 10..21: the first six have less than seven observations.
        self.assertEqual(values(points)[:6], [None] * 6)
        # The seventh row holds rows 1..7, which are closes 10..16 -> 91 / 7.
        self.assertEqual(values(points)[6], "13.00000000")
        # And the last row holds rows 6..12, a full window: 15..21 -> 126 / 7.
        self.assertEqual(values(points)[-1], "18.00000000")

    def test_a_window_longer_than_the_history_reports_holes_not_a_short_mean(self):
        result = services.moving_averages_for_instrument(
            self.twelve, names=("moving_average_30",)
        )

        self.assertEqual(values(result.named("moving_average_30").points), [None] * 12)

    def test_the_pad_covers_the_longest_series_asked_for(self):
        """The read is one, so its warm-up is the longest window's: the 7-day series
        reaches six rows back and the 90-day one eighty-nine, and a request for both
        cannot read six and eighty-nine at once."""
        start = DAY1 + timedelta(days=11)

        short = services.moving_averages_for_instrument(
            self.twelve, names=("moving_average_7",), start_date=start
        )
        long = services.moving_averages_for_instrument(
            self.twelve, names=("moving_average_90",), start_date=start
        )

        # Eleven rows exist before the last one, so the 7-row pad is fully satisfied
        # and the 89-row pad is bounded by the history -- the difference is visible.
        self.assertEqual(short.padding, 6)
        self.assertEqual(long.padding, 11)
        self.assertEqual(len(short.named("moving_average_7").points), 1)
        self.assertEqual(len(long.named("moving_average_90").points), 1)

    def test_the_warm_up_makes_the_first_requested_date_answerable(self):
        result = services.moving_averages_for_instrument(
            self.twelve, names=("moving_average_7",), start_date=DAY3, end_date=DAY5
        )

        self.assertEqual(result.padding, 2)
        self.assertEqual(
            [point.trading_date for point in result.named("moving_average_7").points],
            [DAY3, DAY4, DAY5],
        )
        self.assertEqual(values(result.named("moving_average_7").points), [None] * 3)
        self.assertEqual(result.undefined, 3)

    def test_reading_the_same_dates_without_the_warm_up_leaves_holes(self):
        """The contrast that shows what ``pad`` buys, measured directly: the same
        arithmetic over a series that starts at DAY3 has nothing to average at
        first."""
        series = load_price_series(self.aapl, start_date=DAY3, end_date=DAY5)
        points = moving_average(series.dates, series.closes, 3)

        self.assertEqual(values(points), [None, None, "102.66666667"])

    def test_the_window_counts_observations_not_calendar_days(self):
        """Seven observations with a three-day hole in them: the mean is of the
        observations, so it exists on the seventh row even though seven calendar days
        did not pass between the first and the last."""
        gapped = create_instrument("GAPPED")
        closes = ("10", "20", "60", "70", "80", "90", "100")
        dates = [
            DAY1,
            DAY1 + timedelta(days=1),
            DAY1 + timedelta(days=4),
            DAY1 + timedelta(days=5),
            DAY1 + timedelta(days=6),
            DAY1 + timedelta(days=7),
            DAY1 + timedelta(days=8),
        ]
        for day, close in zip(dates, closes):
            add_price(gapped, day, close, close, close, close, 1)

        result = services.moving_averages_for_instrument(
            gapped, names=("moving_average_7",)
        )
        points = result.named("moving_average_7").points

        self.assertEqual([point.trading_date for point in points], dates)
        self.assertEqual(values(points), [None] * 6 + ["61.42857143"])
        self.assertEqual((dates[-1] - dates[0]).days, 8)

    def test_a_subset_is_a_named_selection(self):
        result = services.moving_averages_for_instrument(
            self.twelve, names=("moving_average_30",)
        )

        self.assertEqual(result.windows, (30,))
        self.assertEqual([named.name for named in result.series], ["moving_average_30"])

    def test_an_empty_selection_computes_nothing_and_reads_nothing(self):
        with self.assertNumQueries(0):
            result = services.moving_averages_for_instrument(self.twelve, names=())

        self.assertEqual(result.series, ())
        self.assertEqual(result.undefined, 0)

    def test_a_series_is_looked_up_by_name_and_an_absent_one_raises(self):
        result = services.moving_averages_for_instrument(
            self.twelve, names=("moving_average_7",)
        )

        self.assertEqual(result.named("moving_average_7").window, 7)
        with self.assertRaises(KeyError) as caught:
            result.named("moving_average_90")
        self.assertIn("moving_average_7", str(caught.exception))

    def test_the_whole_request_is_one_query(self):
        with self.assertNumQueries(1):
            services.moving_averages_for_instrument(self.twelve, start_date=DAY3)

    def test_an_instrument_without_prices_returns_no_points(self):
        empty = create_instrument("EMPTY")

        result = services.moving_averages_for_instrument(
            empty, names=("moving_average_7",)
        )

        self.assertEqual(result.named("moving_average_7").points, [])
        self.assertEqual(result.undefined, 0)

    def test_an_unknown_name_is_rejected_by_name(self):
        with self.assertRaises(ValueError) as caught:
            services.moving_averages_for_instrument(
                self.twelve, names=("moving_average_50",)
            )

        self.assertIn("moving_average_50", str(caught.exception))

    def test_an_unknown_backend_is_rejected_by_name(self):
        with self.assertRaises(ValueError) as caught:
            services.moving_averages_for_instrument(self.twelve, backend="prophet")

        self.assertIn("prophet", str(caught.exception))
        self.assertIn("sql", str(caught.exception))
