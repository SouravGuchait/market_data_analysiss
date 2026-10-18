"""Price extremes: the running high and low, and the range's two pairs.

Closes 100, 102, 101, 103, 104 with traded highs 105, 106, 103, 107, 105 and lows
98, 99, 95, 101, 100 -- deliberately chosen so the two readings of "the highest
price" give different answers:

===========  -------  -------  -----------  ----------  ----------
trading day  close    high     low          highest      lowest
                                              so far       so far
===========  -------  -------  -----------  ----------  ----------
2024-03-11   100      105      98           100 (new)    100 (new)
2024-03-12   102      106      99           102 (new)    100
2024-03-13   101      103      95           102          100
2024-03-14   103      107      101          103 (new)    100
2024-03-15   104      105      100          104 (new)    100
===========  -------  -------  -----------  ----------  ----------

The summary is four numbers from two different columns:

==============================  -------  ------------
quantity                        value    date
==============================  -------  ------------
``highest_close``               104      2024-03-15
``lowest_close``                100      2024-03-11
``highest_high``                107      2024-03-14
``lowest_low``                  95       2024-03-13
``traded_range`` (107 - 95)     12       --
==============================  -------  ------------

Note what that table says: the highest **close** happens on a different day from
the highest **high** (the 107 of the 14th gave back to 105 on the 15th), and the
lowest close of the period is *higher* than the lowest low of the period, which
belongs to the 13th. Neither pair implies the other, which is why both are
reported.
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
    ExtremesSummary,
    extreme_series,
    summarize_extremes,
)

SAMPLE_DATES = [DAY1, DAY2, DAY3, DAY4, DAY5]
SWING_DATES = [DAY1 + timedelta(days=offset) for offset in range(5)]

SWING_CLOSES = ["100", "102", "101", "103", "104"]
SWING_HIGHS = ["105", "106", "103", "107", "105"]
SWING_LOWS = ["98", "99", "95", "101", "100"]


def decimals(values):
    return [Decimal(value) for value in values]


def build_swing():
    """The instrument the module docstring describes, with distinct high/low."""
    instrument = create_instrument("SWING")
    for offset, (close, high, low) in enumerate(
        zip(SWING_CLOSES, SWING_HIGHS, SWING_LOWS)
    ):
        add_price(
            instrument,
            DAY1 + timedelta(days=offset),
            close,
            high,
            low,
            close,
            100,
        )
    return instrument


class ExtremeSeriesUnitTests(SimpleTestCase):
    def test_the_running_high_and_low_at_every_date(self):
        points = extreme_series(SWING_DATES, decimals(SWING_CLOSES))

        # Compared as Decimals, not through ``format_decimal``: a price is money
        # and is passed through at the store's precision, not rendered to eight
        # places, which is a ratio's convention.
        self.assertEqual(
            [point.highest_close for point in points],
            decimals(["100", "102", "102", "103", "104"]),
        )
        self.assertEqual(
            [point.lowest_close for point in points], decimals(["100"] * 5)
        )

    def test_the_peak_date_stays_at_the_first_date_that_reached_the_level(self):
        points = extreme_series(SWING_DATES, decimals(SWING_CLOSES))

        self.assertEqual(
            [point.highest_close_date for point in points],
            [DAY1, DAY2, DAY2, DAY4, DAY5],
        )
        self.assertEqual([point.lowest_close_date for point in points], [DAY1] * 5)

    def test_the_first_point_is_both_the_highest_and_the_lowest_so_far(self):
        first = extreme_series(SWING_DATES, decimals(SWING_CLOSES))[0]

        self.assertTrue(first.is_new_high)
        self.assertTrue(first.is_new_low)
        self.assertEqual(first.highest_close, Decimal("100"))
        self.assertEqual(first.lowest_close, Decimal("100"))

    def test_the_new_extreme_flags_mark_exactly_the_rows_that_moved_one(self):
        points = extreme_series(SWING_DATES, decimals(SWING_CLOSES))

        self.assertEqual([point.is_new_high for point in points], [True, True, False, True, True])
        self.assertEqual([point.is_new_low for point in points], [True, False, False, False, False])

    def test_a_repeated_high_is_not_a_new_one(self):
        closes = decimals(["100", "100", "90"])
        points = extreme_series(SWING_DATES[:3], closes)

        self.assertFalse(points[1].is_new_high)
        self.assertEqual(points[1].highest_close_date, DAY1)

    def test_a_supplied_extreme_is_used_instead_of_the_scan(self):
        points = extreme_series(
            SWING_DATES,
            decimals(SWING_CLOSES),
            highest=decimals(["999"] * 5),
            lowest=decimals(["1"] * 5),
        )

        self.assertEqual(points[0].highest_close, Decimal("999"))
        self.assertEqual(points[0].lowest_close, Decimal("1"))
        # The flags describe the *closes*, not the supplied column: a constant
        # supplied maximum does not make every row a new high.
        self.assertEqual(
            [point.is_new_high for point in points],
            [True, True, False, True, True],
        )

    def test_supplied_extremes_that_are_the_running_ones_agree_with_the_scan(self):
        scanned = extreme_series(SWING_DATES, decimals(SWING_CLOSES))
        supplied = extreme_series(
            SWING_DATES,
            decimals(SWING_CLOSES),
            highest=[point.highest_close for point in scanned],
            lowest=[point.lowest_close for point in scanned],
        )

        self.assertEqual(scanned, supplied)

    def test_mismatched_lengths_are_rejected(self):
        with self.assertRaises(ValueError):
            extreme_series(SWING_DATES, decimals(SWING_CLOSES[:2]))
        with self.assertRaises(ValueError):
            extreme_series(SWING_DATES, decimals(SWING_CLOSES), highest=[Decimal("1")])
        with self.assertRaises(ValueError):
            extreme_series(SWING_DATES, decimals(SWING_CLOSES), lowest=[Decimal("1")])

    def test_an_empty_series_is_an_empty_answer(self):
        self.assertEqual(extreme_series([], []), [])


class ExtremesSummaryUnitTests(SimpleTestCase):
    def setUp(self):
        self.points = extreme_series(SWING_DATES, decimals(SWING_CLOSES))
        self.summary = summarize_extremes(
            SWING_DATES,
            self.points,
            highs=decimals(SWING_HIGHS),
            lows=decimals(SWING_LOWS),
        )

    def test_the_close_extremes_and_their_dates(self):
        self.assertEqual(self.summary.highest_close, Decimal("104"))
        self.assertEqual(self.summary.highest_close_date, DAY5)
        self.assertEqual(self.summary.lowest_close, Decimal("100"))
        self.assertEqual(self.summary.lowest_close_date, DAY1)

    def test_the_traded_extremes_and_their_dates(self):
        self.assertEqual(self.summary.highest_high, Decimal("107"))
        self.assertEqual(self.summary.highest_high_date, DAY4)
        self.assertEqual(self.summary.lowest_low, Decimal("95"))
        self.assertEqual(self.summary.lowest_low_date, DAY3)

    def test_the_two_readings_disagree_which_is_the_point_of_reporting_both(self):
        """The highest close and the highest high are different days, and the
        lowest close is not the lowest low."""
        self.assertNotEqual(
            self.summary.highest_close_date, self.summary.highest_high_date
        )
        self.assertNotEqual(self.summary.lowest_close_date, self.summary.lowest_low_date)
        self.assertGreater(self.summary.lowest_close, self.summary.lowest_low)

    def test_the_traded_range_is_the_width_of_the_period_not_of_a_row(self):
        self.assertEqual(self.summary.traded_range, Decimal("12"))
        # No single row spans 12 on this series.
        row_ranges = [
            Decimal(high) - Decimal(low)
            for high, low in zip(SWING_HIGHS, SWING_LOWS)
        ]
        self.assertNotIn(Decimal("12"), row_ranges)

    def test_the_close_pair_is_a_direct_max_and_min_of_the_closes(self):
        """The summary is taken from the running series; this is the aggregate it
        must equal, written independently."""
        closes = decimals(SWING_CLOSES)

        self.assertEqual(self.summary.highest_close, max(closes))
        self.assertEqual(self.summary.lowest_close, min(closes))
        self.assertEqual(
            self.summary.highest_close_date,
            SWING_DATES[closes.index(self.summary.highest_close)],
        )

    def test_the_summary_is_the_last_points_running_extremes(self):
        self.assertEqual(self.summary.highest_close, self.points[-1].highest_close)
        self.assertEqual(self.summary.lowest_close, self.points[-1].lowest_close)

    def test_a_close_only_series_has_no_traded_extremes(self):
        """Reporting the closes under the traded name would be the worst answer:
        they are a different quantity."""
        summary = summarize_extremes(SWING_DATES, self.points)

        self.assertEqual(summary.highest_close, Decimal("104"))
        self.assertIsNone(summary.highest_high)
        self.assertIsNone(summary.lowest_low)
        self.assertIsNone(summary.traded_range)

    def test_a_tie_reports_the_first_date_that_reached_the_extreme(self):
        summary = summarize_extremes(
            SWING_DATES,
            self.points,
            highs=decimals(["105", "106", "103", "107", "107"]),
            lows=decimals(SWING_LOWS),
        )

        self.assertEqual(summary.highest_high, Decimal("107"))
        self.assertEqual(summary.highest_high_date, DAY4)

    def test_an_empty_series_has_an_empty_summary(self):
        self.assertEqual(summarize_extremes([], []), ExtremesSummary())


class ExtremeBackendAgreementTests(TestCase):
    def test_the_three_backends_agree_on_the_sample_series(self):
        instruments = load_sample_series()

        for symbol in ("AAPL", "MSFT", "GOOG"):
            with self.subTest(symbol=symbol):
                results = {
                    backend: services.price_extremes_for_instrument(
                        instruments[symbol], backend=backend
                    )
                    for backend in services.EXTREME_BACKENDS
                }

                reference = results["python"]
                for backend, result in results.items():
                    self.assertEqual(
                        result.points, reference.points, f"{backend} points differ"
                    )
                    self.assertEqual(
                        result.summary, reference.summary, f"{backend} summary differs"
                    )

    def test_the_three_backends_agree_when_the_traded_pair_differs_from_the_close_pair(self):
        swing = build_swing()

        results = [
            services.price_extremes_for_instrument(swing, backend=backend)
            for backend in services.EXTREME_BACKENDS
        ]

        for result in results:
            self.assertEqual(result.points, results[0].points)
            self.assertEqual(result.summary, results[0].summary)
            self.assertEqual(result.summary.highest_high_date, DAY4)
            self.assertEqual(result.summary.lowest_low_date, DAY3)


class ExtremeServiceTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()
        self.aapl = self.instruments["AAPL"]

    def test_the_running_series_ends_at_the_ranges_extremes(self):
        result = services.price_extremes_for_instrument(self.aapl)

        self.assertEqual(
            [point.highest_close for point in result.points],
            decimals(["100", "102", "102", "103", "104"]),
        )
        self.assertEqual(result.summary.highest_close, Decimal("104"))
        self.assertEqual(result.summary.highest_close_date, DAY5)
        self.assertEqual(result.summary.lowest_close, Decimal("100"))
        self.assertEqual(result.summary.lowest_close_date, DAY1)

    def test_the_extremes_are_the_ranges_own_not_the_historys(self):
        """As with the drawdown: DAY1's 100 is the lowest close of the table, and a
        request starting on DAY3 must not see it."""
        whole = services.price_extremes_for_instrument(self.aapl)
        from_day3 = services.price_extremes_for_instrument(self.aapl, start_date=DAY3)

        self.assertEqual(whole.summary.lowest_close, Decimal("100"))
        self.assertEqual(whole.summary.lowest_close_date, DAY1)

        self.assertEqual(from_day3.summary.lowest_close, Decimal("101"))
        self.assertEqual(from_day3.summary.lowest_close_date, DAY3)
        self.assertEqual(from_day3.summary.highest_close, Decimal("104"))
        self.assertEqual(from_day3.padding, 0)

    def test_no_warm_up_row_is_read(self):
        result = services.price_extremes_for_instrument(self.aapl, start_date=DAY3)

        self.assertEqual(result.padding, 0)
        self.assertEqual([point.trading_date for point in result.points], [DAY3, DAY4, DAY5])

    def test_the_traded_pair_is_read_from_the_high_and_low_columns(self):
        result = services.price_extremes_for_instrument(self.aapl)

        # The sample fixture stores high = low = close, so the two readings coincide
        # here -- which is a fact about the fixture, asserted rather than assumed.
        self.assertEqual(result.summary.highest_high, result.summary.highest_close)
        self.assertEqual(result.summary.lowest_low, result.summary.lowest_close)
        self.assertEqual(result.summary.traded_range, Decimal("4"))

    def test_the_traded_pair_differs_from_the_close_pair_on_a_real_series(self):
        swing = build_swing()

        result = services.price_extremes_for_instrument(swing)

        self.assertEqual(result.summary.highest_high, Decimal("107"))
        self.assertEqual(result.summary.highest_high_date, DAY4)
        self.assertEqual(result.summary.highest_close, Decimal("104"))
        self.assertEqual(result.summary.highest_close_date, DAY5)
        self.assertEqual(result.summary.lowest_low, Decimal("95"))
        self.assertEqual(result.summary.lowest_low_date, DAY3)
        self.assertEqual(result.summary.lowest_close, Decimal("100"))
        self.assertEqual(result.summary.traded_range, Decimal("12"))

    def test_the_whole_request_is_one_query(self):
        with self.assertNumQueries(1):
            services.price_extremes_for_instrument(self.aapl, start_date=DAY2)

    def test_the_range_bounds_the_rows(self):
        result = services.price_extremes_for_instrument(
            self.aapl, start_date=DAY2, end_date=DAY4
        )

        self.assertEqual([point.trading_date for point in result.points], [DAY2, DAY3, DAY4])
        self.assertEqual(result.summary.highest_close, Decimal("103"))

    def test_nothing_is_undefined_because_every_row_has_a_close(self):
        result = services.price_extremes_for_instrument(self.aapl)

        self.assertEqual(result.undefined, 0)

    def test_an_instrument_without_prices_returns_no_points(self):
        empty = create_instrument("EMPTY")

        result = services.price_extremes_for_instrument(empty)

        self.assertEqual(result.points, [])
        self.assertEqual(result.summary, ExtremesSummary())
        self.assertEqual(result.undefined, 0)

    def test_an_unknown_backend_is_rejected_by_name(self):
        with self.assertRaises(ValueError) as caught:
            services.price_extremes_for_instrument(self.aapl, backend="prophet")

        self.assertIn("prophet", str(caught.exception))
        self.assertIn("sql", str(caught.exception))
