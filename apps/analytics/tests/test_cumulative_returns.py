"""Cumulative returns: the chain, its base, and what breaks it.

The arithmetic, for ``AAPL`` (100, 102, 101, 103, 104), with the base at 100:

===========  =======  ============  ==============
trading day  close    growth        cumulative
===========  =======  ============  ==============
2024-03-11   100      100/100 = 1   0.00
2024-03-12   102      102/100 = 1.02 +2.00%
2024-03-13   101      101/100 = 1.01 +1.00%
2024-03-14   103      103/100 = 1.03 +3.00%
2024-03-15   104      104/100 = 1.04 +4.00%
===========  =======  ============  ==============

The last figure is the range's total return, which is the number
``apps/marketdata/tests/helpers.py`` already documents as ``EXPECTED_RETURN``:
4% for AAPL, 5% for MSFT, 8% for GOOG. That fixture was written for the period
return, and this metric must reproduce it -- the same question asked two ways.
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
    EXPECTED_RETURN,
    add_price,
    create_instrument,
    load_sample_series,
)

from .. import services
from ..metrics import (
    CumulativePoint,
    cumulative_returns,
    cumulative_returns_pandas,
    daily_returns,
    format_decimal,
)
from ..series import load_price_series

AAPL_GROWTH = ["1.00000000", "1.02000000", "1.01000000", "1.03000000", "1.04000000"]
AAPL_VALUES = ["0.00000000", "0.02000000", "0.01000000", "0.03000000", "0.04000000"]
MSFT_VALUES = ["0.00000000", "-0.05000000", "-0.02500000", "0.02500000", "0.05000000"]
GOOG_VALUES = ["0.00000000", "0.02000000", "0.04000000", "0.06000000", "0.08000000"]

SAMPLE_DATES = [DAY1, DAY2, DAY3, DAY4, DAY5]


def growth_series(points):
    return [format_decimal(point.growth) for point in points]


def value_series(points):
    return [format_decimal(point.value) for point in points]


class CumulativeReturnsUnitTests(SimpleTestCase):
    """The definition, with no database."""

    def test_the_growth_and_the_return_of_a_hand_computed_series(self):
        closes = [Decimal(value) for value in ("100", "102", "101", "103", "104")]

        points = cumulative_returns(SAMPLE_DATES, closes)

        self.assertEqual(growth_series(points), AAPL_GROWTH)
        self.assertEqual(value_series(points), AAPL_VALUES)

    def test_the_base_itself_is_zero_rather_than_undefined(self):
        """Unlike a daily return, the first value is a real number: nothing has
        happened yet, which is 0%, not "unknown"."""
        points = cumulative_returns([DAY1], [Decimal("100")])

        self.assertEqual(points[0].growth, Decimal(1))
        self.assertEqual(format_decimal(points[0].value), "0.00000000")
        self.assertEqual(points[0].base_close, Decimal("100"))

    def test_each_point_carries_the_base_it_was_measured_from(self):
        points = cumulative_returns(SAMPLE_DATES, [Decimal(v) for v in ("100", "102", "101", "103", "104")])

        self.assertEqual({point.base_close for point in points}, {Decimal("100")})

    def test_the_product_of_the_reported_returns_reproduces_the_growth(self):
        """The cross-check: the two definitions are the same number.

        The engine divides the closes (exact); the definition is the compounded
        product. Compounding the *reported* returns carries the eight-place
        rounding once per day, so the bound is the number of days times the
        quantum -- and it must hold, or one of the two routes is wrong.
        """
        closes = [Decimal(value) for value in ("100", "102", "101", "103", "104")]
        points = cumulative_returns(SAMPLE_DATES, closes)
        returns = daily_returns(SAMPLE_DATES, closes)

        product = Decimal(1)
        for point, step in zip(points, returns):
            if step.value is not None:
                product = product * (1 + step.value)

            bound = Decimal(len(SAMPLE_DATES)) * Decimal("0.00000001")
            self.assertLessEqual(abs(product - point.growth), bound)

    def test_a_flat_price_gives_a_flat_chain(self):
        points = cumulative_returns(SAMPLE_DATES, [Decimal("7")] * 5)

        self.assertEqual(value_series(points), ["0.00000000"] * 5)

    def test_an_undefined_return_breaks_the_chain_from_that_date_on(self):
        """A zero close is a real possibility, and there is no honest value after
        it: the index cannot be carried through a division by zero."""
        points = cumulative_returns(
            [DAY1, DAY2, DAY3], [Decimal("10"), Decimal("0"), Decimal("5")]
        )

        # Losing the whole price is a defined return of -100%; the *next* step
        # divides by the zero close, and that is what breaks.
        self.assertEqual(growth_series(points), ["1.00000000", "0.00000000", None])
        self.assertEqual(value_series(points), ["0.00000000", "-1.00000000", None])

    def test_a_series_that_starts_at_zero_is_defined_at_the_base_and_nowhere_after(self):
        points = cumulative_returns([DAY1, DAY2], [Decimal("0"), Decimal("5")])

        self.assertEqual(value_series(points), ["0.00000000", None])

    def test_an_empty_series_is_an_empty_answer(self):
        self.assertEqual(cumulative_returns([], []), [])

    def test_mismatched_lengths_are_rejected(self):
        with self.assertRaises(ValueError):
            cumulative_returns([DAY1, DAY2], [Decimal("1")])

    def test_points_compare_by_value_not_by_identity(self):
        points = cumulative_returns([DAY1], [Decimal("100")])

        self.assertEqual(
            points[0],
            CumulativePoint(
                trading_date=DAY1,
                close=Decimal("100"),
                base_close=Decimal("100"),
                growth=Decimal(1),
                value=Decimal("0.00000000"),
            ),
        )


class CumulativeBackendAgreementTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()

    def test_the_backends_agree_point_for_point(self):
        for symbol in ("AAPL", "MSFT", "GOOG"):
            with self.subTest(symbol=symbol):
                series = load_price_series(self.instruments[symbol])

                self.assertEqual(
                    cumulative_returns(series.dates, series.closes),
                    cumulative_returns_pandas(series.dates, series.closes),
                )

    def test_the_backends_agree_on_a_series_whose_chain_breaks(self):
        instrument = create_instrument("ZEROED")
        for offset, close in enumerate(["10", "0", "5"]):
            add_price(
                instrument,
                DAY1 + timedelta(days=offset),
                close,
                close,
                close,
                close,
                1,
            )

        series = load_price_series(instrument)

        self.assertEqual(
            cumulative_returns(series.dates, series.closes),
            cumulative_returns_pandas(series.dates, series.closes),
        )


class CumulativeReturnsServiceTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()
        self.aapl = self.instruments["AAPL"]

    def test_the_total_is_the_period_return_the_fixtures_already_document(self):
        """``EXPECTED_RETURN`` is the pre-existing period-return fixture.

        Cumulative returns must agree with it, because it answers the same
        question: ``close(last) / close(first) - 1``.
        """
        for symbol, expected in EXPECTED_RETURN.items():
            with self.subTest(symbol=symbol):
                result = services.cumulative_returns_for_instrument(self.instruments[symbol])

                self.assertEqual(format_decimal(result.total), f"{Decimal(expected):.8f}")

    def test_the_series_over_a_range_is_the_running_return_inside_it(self):
        result = services.cumulative_returns_for_instrument(self.aapl)

        self.assertEqual(value_series(result.points), AAPL_VALUES)
        self.assertEqual(growth_series(result.points), AAPL_GROWTH)
        self.assertEqual(result.undefined, 0)
        self.assertEqual(result.padding, 0)

    def test_the_base_is_the_first_requested_date_not_the_series_first_date(self):
        """The range's own return: starting at DAY3, the chain starts at 0 on
        DAY3 rather than reporting DAY1's move into the range."""
        result = services.cumulative_returns_for_instrument(self.aapl, start_date=DAY3)

        self.assertEqual([point.trading_date for point in result.points], [DAY3, DAY4, DAY5])
        self.assertEqual(value_series(result.points), ["0.00000000", "0.01980198", "0.02970297"])
        # 103/101 - 1 = 1.980198% ; 104/101 - 1 = 2.970297%
        self.assertEqual(format_decimal(result.total), "0.02970297")

    def test_a_bounded_range_reads_no_warm_up_row(self):
        result = services.cumulative_returns_for_instrument(
            self.aapl, start_date=DAY2, end_date=DAY4
        )

        self.assertEqual(result.padding, 0)
        self.assertEqual([point.trading_date for point in result.points], [DAY2, DAY3, DAY4])

    def test_the_whole_request_is_one_query(self):
        with self.assertNumQueries(1):
            services.cumulative_returns_for_instrument(self.aapl, start_date=DAY2)

    def test_an_instrument_without_prices_returns_no_points_and_no_total(self):
        empty = create_instrument("EMPTY")

        result = services.cumulative_returns_for_instrument(empty)

        self.assertEqual(result.points, [])
        self.assertIsNone(result.total)

    def test_a_broken_chain_has_no_total(self):
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

        result = services.cumulative_returns_for_instrument(zeroed)

        self.assertEqual(result.undefined, 1)
        self.assertIsNone(result.total)

    def test_there_is_no_sql_backend_and_saying_so_is_explicit(self):
        """SQL has no windowed product, so the engine does not pretend to have
        one; the rejection names what is available."""
        with self.assertRaises(ValueError) as caught:
            services.cumulative_returns_for_instrument(self.aapl, backend="sql")

        self.assertIn("sql", str(caught.exception))
        self.assertIn("pandas", str(caught.exception))
