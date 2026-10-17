"""Maximum drawdown: the peak each date is measured from, and the worst episode.

The running maximum, and the decline below it, on a hand-computed series
(closes 100, 120, 90, 110, 80, 130):

===========  -------  -------  ----------------  --------------
trading day  close    peak     calculation       drawdown
===========  -------  -------  ----------------  --------------
2024-03-11   100      100      new high          0.00000000
2024-03-12   120      120      new high          0.00000000
2024-03-13   90       120      90 / 120 - 1      -0.25000000
2024-03-14   110      120      110 / 120 - 1     -0.08333333
2024-03-15   80       120      80 / 120 - 1      -0.33333333
2024-03-16   130      130      new high          0.00000000
===========  -------  -------  ----------------  --------------

The worst episode is the ``-0.33333333`` on 2024-03-15, measured from the 120 of
2024-03-12, and it is made back on 2024-03-16 (130 >= 120).

The standard sample series, where the drawdown is small and the recovery is
immediate:

===========  -------  -------  ----------------  --------------
trading day  close    peak     calculation       drawdown
===========  -------  -------  ----------------  --------------
2024-03-11   100      100      new high          0.00000000
2024-03-12   102      102      new high          0.00000000
2024-03-13   101      102      101 / 102 - 1     -0.00980392
2024-03-14   103      103      new high          0.00000000
2024-03-15   104      104      new high          0.00000000
===========  -------  -------  ----------------  --------------

so the worst episode is ``-0.00980392``, from the 102 of DAY2 to the 101 of DAY3,
made back on DAY4.
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
    DrawdownSummary,
    drawdown_series,
    format_decimal,
    summarize_drawdown,
)

SAMPLE_DATES = [DAY1, DAY2, DAY3, DAY4, DAY5]

#: Six consecutive sessions, because five highs and troughs need room.
SWING_DATES = [DAY1 + timedelta(days=offset) for offset in range(6)]
SWING_CLOSES = [Decimal(value) for value in ("100", "120", "90", "110", "80", "130")]

AAPL_DRAWDOWNS = ["0.00000000", "0.00000000", "-0.00980392", "0.00000000", "0.00000000"]


def values(points):
    return [format_decimal(point.value) for point in points]


def peaks(points):
    return [point.peak for point in points]


class DrawdownSeriesUnitTests(SimpleTestCase):
    def test_the_hand_computed_series(self):
        points = drawdown_series(SWING_DATES, SWING_CLOSES)

        self.assertEqual(
            values(points),
            [
                "0.00000000",
                "0.00000000",
                "-0.25000000",
                "-0.08333333",
                "-0.33333333",
                "0.00000000",
            ],
        )
        self.assertEqual(
            peaks(points),
            [Decimal(value) for value in ("100", "120", "120", "120", "120", "130")],
        )

    def test_every_point_names_the_peak_it_is_measured_from(self):
        points = drawdown_series(SWING_DATES, SWING_CLOSES)

        self.assertEqual(
            [point.peak_date for point in points],
            [DAY1, SWING_DATES[1], SWING_DATES[1], SWING_DATES[1], SWING_DATES[1], SWING_DATES[5]],
        )

    def test_a_new_high_is_a_zero_drawdown_rather_than_a_missing_one(self):
        """Nothing was given up, which is a value; the peak has moved with it."""
        points = drawdown_series(SWING_DATES, SWING_CLOSES)

        self.assertEqual(points[0].value, Decimal(0))
        self.assertEqual(points[0].peak, Decimal("100"))
        self.assertEqual(points[5].value, Decimal(0))
        self.assertEqual(points[5].peak, Decimal("130"))

    def test_a_tie_does_not_restart_the_peak_even_though_it_is_a_new_maximum_row(self):
        """Two dates at the same high: the episode starts at the first."""
        closes = [Decimal(value) for value in ("100", "100", "90")]

        points = drawdown_series(SWING_DATES[:3], closes)

        self.assertEqual(points[1].peak_date, DAY1)
        self.assertEqual(values(points), ["0.00000000", "0.00000000", "-0.10000000"])

    def test_a_zero_peak_has_no_drawdown_to_report(self):
        """``close / 0`` is not a ratio, so the value is None, never 0 or inf.

        The next close is a new high, so the peak becomes positive and the metric
        resumes; the hole is only where the peak was zero.
        """
        closes = [Decimal(value) for value in ("0", "10", "5")]

        points = drawdown_series(SWING_DATES[:3], closes)

        self.assertEqual(values(points), [None, "0.00000000", "-0.50000000"])
        self.assertEqual(points[0].peak, Decimal("0"))
        self.assertEqual(points[1].peak_date, SWING_DATES[1])

    def test_a_supplied_peak_is_used_instead_of_the_scan(self):
        supplied = [Decimal("999"), Decimal("120"), Decimal("120"), Decimal("120"), Decimal("120"), Decimal("130")]

        points = drawdown_series(SWING_DATES, SWING_CLOSES, peaks=supplied)

        self.assertEqual(points[0].peak, Decimal("999"))
        # 100 / 999 - 1, not 0: the supplied peak really is the one used.
        self.assertEqual(values(points)[0], "-0.89989990")

    def test_supplied_peaks_that_are_the_running_maximum_agree_with_the_scan(self):
        scanned = drawdown_series(SWING_DATES, SWING_CLOSES)
        supplied = drawdown_series(
            SWING_DATES, SWING_CLOSES, peaks=[point.peak for point in scanned]
        )

        self.assertEqual(scanned, supplied)

    def test_a_supplied_zero_peak_obeys_the_same_rule(self):
        points = drawdown_series(
            SWING_DATES[:3],
            SWING_CLOSES[:3],
            peaks=[Decimal("0"), Decimal("120"), Decimal("120")],
        )

        self.assertEqual(values(points), [None, "0.00000000", "-0.25000000"])

    def test_mismatched_lengths_are_rejected(self):
        with self.assertRaises(ValueError):
            drawdown_series(SWING_DATES, SWING_CLOSES[:2])
        with self.assertRaises(ValueError):
            drawdown_series(SWING_DATES, SWING_CLOSES, peaks=[Decimal("1")])

    def test_an_empty_series_is_an_empty_answer(self):
        self.assertEqual(drawdown_series([], []), [])


class DrawdownSummaryUnitTests(SimpleTestCase):
    def test_the_worst_episode_and_its_recovery(self):
        summary = summarize_drawdown(drawdown_series(SWING_DATES, SWING_CLOSES))

        self.assertEqual(format_decimal(summary.value), "-0.33333333")
        self.assertEqual(summary.peak_date, SWING_DATES[1])
        self.assertEqual(summary.trough_date, SWING_DATES[4])
        self.assertEqual(summary.peak_close, Decimal("120"))
        self.assertEqual(summary.trough_close, Decimal("80"))
        self.assertIs(summary.recovered, True)
        self.assertEqual(summary.recovery_date, SWING_DATES[5])

    def test_a_decline_still_under_water_at_the_end_is_not_recovered(self):
        closes = [Decimal(value) for value in ("100", "80", "90")]

        summary = summarize_drawdown(drawdown_series(SWING_DATES[:3], closes))

        self.assertEqual(summary.recovered, False)
        self.assertIsNone(summary.recovery_date)
        self.assertEqual(format_decimal(summary.value), "-0.20000000")

    def test_a_series_that_never_fell_reports_zero_and_no_episode(self):
        closes = [Decimal(value) for value in ("50", "51", "52")]

        summary = summarize_drawdown(drawdown_series(SWING_DATES[:3], closes))

        self.assertEqual(format_decimal(summary.value), "0.00000000")
        self.assertIsNone(summary.peak_date)
        self.assertIsNone(summary.trough_date)
        self.assertIsNone(summary.recovered)

    def test_equal_depth_episodes_report_the_first(self):
        """A 10% drawdown twice is one number, and the earlier one is it."""
        closes = [Decimal(value) for value in ("100", "90", "100", "90")]

        summary = summarize_drawdown(drawdown_series(SWING_DATES[:4], closes))

        self.assertEqual(summary.peak_date, DAY1)
        self.assertEqual(summary.trough_date, SWING_DATES[1])
        self.assertEqual(summary.trough_close, Decimal("90"))
        self.assertEqual(summary.recovery_date, SWING_DATES[2])

    def test_an_all_undefined_series_has_no_summary(self):
        closes = [Decimal("0"), Decimal("0")]

        summary = summarize_drawdown(drawdown_series(SWING_DATES[:2], closes))

        self.assertEqual(summary, DrawdownSummary())
        self.assertIsNone(summary.value)
        self.assertIsNone(summary.recovered)

    def test_the_summary_of_nothing_is_all_none(self):
        self.assertEqual(summarize_drawdown([]), DrawdownSummary())


class DrawdownBackendAgreementTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()

    def test_the_three_backends_agree_on_the_sample_series(self):
        for symbol in ("AAPL", "MSFT", "GOOG"):
            with self.subTest(symbol=symbol):
                results = {
                    backend: services.maximum_drawdown_for_instrument(
                        self.instruments[symbol], backend=backend
                    )
                    for backend in services.DRAWDOWN_BACKENDS
                }

                reference = results["python"]
                for backend, result in results.items():
                    self.assertEqual(
                        result.points, reference.points, f"{backend} points differ"
                    )
                    self.assertEqual(
                        result.summary, reference.summary, f"{backend} summary differs"
                    )

    def test_the_backends_agree_on_a_series_with_a_zero_peak(self):
        zeroed = create_instrument("ZEROED")
        for offset, close in enumerate(["0", "10", "5", "1"]):
            add_price(
                zeroed,
                DAY1 + timedelta(days=offset),
                close,
                close,
                close,
                close,
                1,
            )

        results = [
            services.maximum_drawdown_for_instrument(zeroed, backend=backend)
            for backend in services.DRAWDOWN_BACKENDS
        ]

        for result in results:
            self.assertEqual(values(result.points), [None, "0.00000000", "-0.50000000", "-0.90000000"])
            self.assertEqual(result.summary.value, Decimal("-0.9"))
            self.assertEqual(result.undefined, 1)


class DrawdownServiceTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()
        self.aapl = self.instruments["AAPL"]

    def test_the_peak_is_the_ranges_own_high_not_the_historys(self):
        """The anchoring, asserted as the difference between two requests.

        DAY2's 102 is the highest close before DAY3; if the peak came from the
        history, DAY3 would open at ``-0.00980392``. Anchored inside the range it
        opens at ``0``, and the whole range of closes 101, 103, 104 never falls.
        """
        whole = services.maximum_drawdown_for_instrument(self.aapl)
        from_day3 = services.maximum_drawdown_for_instrument(self.aapl, start_date=DAY3)

        self.assertEqual(values(whole.points), AAPL_DRAWDOWNS)
        self.assertEqual(format_decimal(whole.summary.value), "-0.00980392")
        self.assertEqual(whole.summary.peak_date, DAY2)

        self.assertEqual(values(from_day3.points), ["0.00000000", "0.00000000", "0.00000000"])
        self.assertEqual(format_decimal(from_day3.summary.value), "0.00000000")
        self.assertEqual(from_day3.padding, 0)

        # Each point still names the peak it is measured from, and those peaks are
        # the range's own; there is no *episode* to report, which is what the
        # summary's None dates say.
        self.assertEqual(
            [point.peak_date for point in from_day3.points], [DAY3, DAY4, DAY5]
        )
        self.assertIsNone(from_day3.summary.peak_date)

    def test_no_warm_up_row_is_read(self):
        result = services.maximum_drawdown_for_instrument(self.aapl, start_date=DAY3)

        self.assertEqual(result.padding, 0)
        self.assertEqual([point.trading_date for point in result.points], [DAY3, DAY4, DAY5])

    def test_the_whole_request_is_one_query(self):
        with self.assertNumQueries(1):
            services.maximum_drawdown_for_instrument(self.aapl, start_date=DAY2)

    def test_the_range_bounds_the_rows(self):
        result = services.maximum_drawdown_for_instrument(
            self.aapl, start_date=DAY2, end_date=DAY4
        )

        self.assertEqual([point.trading_date for point in result.points], [DAY2, DAY3, DAY4])

    def test_the_sequence_of_trading_days_is_not_interval_arithmetic(self):
        """A four-day hole: the drawdown is measured across observations, and the
        trough is the next observation rather than the next calendar day."""
        gapped = create_instrument("GAPPED")
        for day, close in ((DAY1, "100"), (DAY2, "120"), (DAY5, "60")):
            add_price(gapped, day, close, close, close, close, 1)

        result = services.maximum_drawdown_for_instrument(gapped)

        self.assertEqual([point.trading_date for point in result.points], [DAY1, DAY2, DAY5])
        self.assertEqual(result.summary.peak_date, DAY2)
        self.assertEqual(result.summary.trough_date, DAY5)
        self.assertEqual((DAY5 - DAY2).days, 3)

    def test_an_instrument_without_prices_returns_no_points(self):
        empty = create_instrument("EMPTY")

        result = services.maximum_drawdown_for_instrument(empty)

        self.assertEqual(result.points, [])
        self.assertEqual(result.summary, DrawdownSummary())

    def test_an_unknown_backend_is_rejected_by_name(self):
        with self.assertRaises(ValueError) as caught:
            services.maximum_drawdown_for_instrument(self.aapl, backend="prophet")

        self.assertIn("prophet", str(caught.exception))
        self.assertIn("sql", str(caught.exception))
