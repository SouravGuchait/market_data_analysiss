"""Daily simple returns: the formula, the edge cases, and the three backends.

Every expected value here is a literal that can be checked by hand. The sample
series is the one ``apps/marketdata/tests/helpers.py`` documents
(``AAPL 100, 102, 101, 103, 104`` on five consecutive weekdays), so the numbers
in this file and the numbers in the exercise tests describe the same rows.

The arithmetic, for ``AAPL``:

===========  ===============  ============  ==============================
trading day  previous close   close         return
===========  ===============  ============  ==============================
2024-03-11   (none)           100.0         null -- nothing to divide by
2024-03-12   100              102           (102 - 100) / 100  =  0.02
2024-03-13   102              101           (101 - 102) / 102  = -0.00980392
2024-03-14   101              103           (103 - 101) / 101  =  0.01980198
2024-03-15   103              104           (104 - 103) / 103  =  0.00970874
===========  ===============  ============  ==============================

The last column is rounded to eight places with ``ROUND_HALF_UP``; the unrounded
values are ``-0.009803921568...``, ``0.019801980198...`` and ``0.009708737864...``.
"""

from datetime import date, timedelta
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
    RATIO_EXPONENT,
    ReturnPoint,
    daily_returns,
    daily_returns_pandas,
    quantize_ratio,
    simple_return,
)
from ..series import load_price_series

#: ``AAPL``'s expected returns, in date order, as exact strings.
AAPL_RETURNS = [
    None,
    "0.02000000",
    "-0.00980392",
    "0.01980198",
    "0.00970874",
]

#: ``MSFT`` (200, 190, 195, 205, 210) -- a falling series, so the sign handling
#: and the ``5/190`` rounding case are covered too.
MSFT_RETURNS = [
    None,
    "-0.05000000",
    "0.02631579",
    "0.05128205",
    "0.02439024",
]

#: ``GOOG`` (50, 51, 52, 53, 54) -- a small, smooth rise.
GOOG_RETURNS = [
    None,
    "0.02000000",
    "0.01960784",
    "0.01923077",
    "0.01886792",
]


def values(points):
    """The return of each point, as a list, for comparison with the literals."""
    return [None if point.value is None else str(point.value) for point in points]


class SimpleReturnUnitTests(SimpleTestCase):
    """The formula and its two undefined cases, with no database."""

    def test_the_formula_is_the_documented_one(self):
        self.assertEqual(simple_return("102", "100"), Decimal("0.02000000"))
        self.assertEqual(simple_return("101", "102"), Decimal("-0.00980392"))
        self.assertEqual(simple_return("103", "101"), Decimal("0.01980198"))
        self.assertEqual(simple_return("104", "103"), Decimal("0.00970874"))

    def test_the_first_observation_has_no_previous_close_and_no_return(self):
        self.assertIsNone(simple_return("100", None))

        points = daily_returns([DAY1], [Decimal("100")])
        self.assertEqual(points[0].previous_close, None)
        self.assertIsNone(points[0].value)

    def test_a_zero_previous_close_is_undefined_rather_than_an_error(self):
        """A zero close is permitted by the schema, so this is reachable data."""
        self.assertIsNone(simple_return("105", "0"))
        self.assertIsNone(simple_return("0", "0"))

    def test_a_negative_return_is_the_mirror_of_the_gain_that_produced_it(self):
        self.assertEqual(simple_return("100", "200"), Decimal("-0.50000000"))
        self.assertEqual(simple_return("200", "100"), Decimal("1.00000000"))

    def test_rounding_is_half_up_away_from_zero_not_bankers(self):
        """A tie at the ninth place must round away from zero, symmetrically.

        ``2.5e-8`` is exactly half of a unit at eight places. Banker's rounding
        (Python's default) would take it to ``0.00000002`` because 2 is even;
        a reader of a reported percentage expects ``0.00000003``.
        """
        self.assertEqual(quantize_ratio(Decimal("0.000000025")), Decimal("0.00000003"))
        self.assertEqual(quantize_ratio(Decimal("-0.000000025")), Decimal("-0.00000003"))

        # The same tie, reached through the formula rather than the quantizer.
        self.assertEqual(
            simple_return("100000002.5", "100000000"), Decimal("0.00000003")
        )

    def test_the_engine_quantum_matches_the_snapshot_column_width(self):
        """One precision in the system: ``DECIMAL(18,8)`` is the store's width."""
        self.assertEqual(RATIO_EXPONENT, Decimal("0.00000001"))
        self.assertEqual(-RATIO_EXPONENT.as_tuple().exponent, 8)

    def test_mismatched_lengths_are_rejected_rather_than_zipped_short(self):
        with self.assertRaises(ValueError):
            daily_returns([DAY1, DAY2], [Decimal("1")])

        with self.assertRaises(ValueError):
            daily_returns([DAY1], [Decimal("1")], previous_closes=[None, None])

    def test_points_carry_the_inputs_as_well_as_the_answer(self):
        points = daily_returns([DAY1, DAY2], [Decimal("100"), Decimal("102")])

        self.assertEqual(
            points[1],
            ReturnPoint(
                trading_date=DAY2,
                close=Decimal("102"),
                previous_close=Decimal("100"),
                value=Decimal("0.02000000"),
            ),
        )


class BackendAgreementTests(TestCase):
    """The three implementations must not disagree, or one of them is wrong."""

    def setUp(self):
        self.instruments = load_sample_series()

    def _sql_points(self, instrument, **kwargs):
        """The database's ``LAG(close)`` plus the Python ratio."""
        series = load_price_series(instrument, pad=1, with_previous_close=True, **kwargs)
        return series, daily_returns(
            series.dates, series.closes, previous_closes=series.column("previous_close")
        )

    def test_the_sql_window_and_the_python_shift_agree_row_for_row(self):
        for symbol in ("AAPL", "MSFT", "GOOG"):
            with self.subTest(symbol=symbol):
                instrument = self.instruments[symbol]
                _, from_sql = self._sql_points(instrument)
                shifted = load_price_series(instrument, pad=1)
                from_python = daily_returns(shifted.dates, shifted.closes)

                self.assertEqual(from_sql, from_python)

    def test_the_window_function_reproduces_the_expected_series(self):
        _, points = self._sql_points(self.instruments["AAPL"])

        self.assertEqual(values(points), AAPL_RETURNS)

    def test_the_window_function_reproduces_the_other_sample_series(self):
        for symbol, expected in (("MSFT", MSFT_RETURNS), ("GOOG", GOOG_RETURNS)):
            with self.subTest(symbol=symbol):
                _, points = self._sql_points(self.instruments[symbol])

                self.assertEqual(values(points), expected)

    def test_pandas_agrees_with_the_decimal_arithmetic_point_by_point(self):
        for symbol in ("AAPL", "MSFT", "GOOG"):
            with self.subTest(symbol=symbol):
                instrument = self.instruments[symbol]
                series = load_price_series(instrument, pad=1)

                decimal_points = daily_returns(series.dates, series.closes)
                pandas_points = daily_returns_pandas(series.dates, series.closes)

                self.assertEqual(decimal_points, pandas_points)

    def test_pandas_pct_change_agrees_once_it_is_quantized(self):
        """``pct_change`` is ``close / previous - 1``, not ``(close - previous) / previous``.

        The two are equal in exact arithmetic and differ in the last bit of a
        float64, which is why the backends are compared at the engine's declared
        precision rather than bit for bit. This test states that claim instead of
        leaving it in a comment.
        """
        import pandas as pd

        closes = pd.Series([100.0, 102.0, 101.0, 103.0, 104.0], dtype="float64")
        pct_change = closes.pct_change(fill_method=None)

        expected = [None, *AAPL_RETURNS[1:]]
        for index, expected_value in enumerate(expected):
            with self.subTest(index=index):
                if expected_value is None:
                    self.assertTrue(pd.isna(pct_change.iloc[index]))
                    continue
                self.assertEqual(
                    quantize_ratio(Decimal(str(pct_change.iloc[index]))),
                    Decimal(expected_value),
                )

    def test_a_zero_close_is_undefined_in_every_backend(self):
        instrument = create_instrument("ZEROED")
        for offset, close in enumerate(["10", "0", "5"]):
            add_price(
                instrument,
                DAY1 + timedelta(days=offset),
                close,
                close,
                close,
                close,
                1000,
            )

        series = load_price_series(instrument, pad=1)
        decimal_points = daily_returns(series.dates, series.closes)
        pandas_points = daily_returns_pandas(series.dates, series.closes)

        self.assertEqual(values(decimal_points), [None, "-1.00000000", None])
        self.assertEqual(decimal_points, pandas_points)


class DailyReturnsServiceTests(TestCase):
    """The entry point the API will call: range, warm-up, and cost."""

    def setUp(self):
        self.instruments = load_sample_series()
        self.aapl = self.instruments["AAPL"]

    def test_the_answer_carries_the_intermediate_calculation(self):
        """``(close - previous_close) / previous_close``, term by term.

        Compared as decimals, not as strings: SQLite and MySQL round-trip a
        ``DECIMAL(18,8)`` with different trailing zeros, and the engine normalises
        through ``Decimal`` so the *value* is backend-independent. The eight-place
        rendering of a return is asserted separately, in
        :func:`values`-based tests.
        """
        result = services.daily_returns_for_instrument(self.aapl)

        self.assertEqual(result.symbol, "AAPL")
        self.assertEqual(
            [
                (point.trading_date, point.previous_close, point.close, point.value)
                for point in result.points
            ],
            [
                (DAY1, None, Decimal("100"), None),
                (DAY2, Decimal("100"), Decimal("102"), Decimal("0.02000000")),
                (DAY3, Decimal("102"), Decimal("101"), Decimal("-0.00980392")),
                (DAY4, Decimal("101"), Decimal("103"), Decimal("0.01980198")),
                (DAY5, Decimal("103"), Decimal("104"), Decimal("0.00970874")),
            ],
        )

    def test_the_warm_up_row_makes_the_first_requested_date_answerable(self):
        """The whole point of reading one row beyond the range.

        Asking for the range alone leaves the first date with nothing behind it,
        so the response would open with a null that the database could have
        answered.
        """
        result = services.daily_returns_for_instrument(
            self.aapl, start_date=DAY2, end_date=DAY5
        )

        self.assertEqual(result.padding, 1)
        self.assertEqual([point.trading_date for point in result.points], [DAY2, DAY3, DAY4, DAY5])
        # DAY2's return is computed from DAY1, which is *not* in the response.
        self.assertEqual(values(result.points), AAPL_RETURNS[1:])

    def test_without_the_warm_up_row_the_first_date_would_have_no_return(self):
        """The same date, read without padding, to show what padding buys."""
        series = load_price_series(self.aapl, start_date=DAY2, end_date=DAY5, pad=0)

        self.assertEqual(series.padding, 0)
        self.assertEqual(values(daily_returns(series.dates, series.closes)), [None, *AAPL_RETURNS[2:]])

    def test_an_unbounded_request_has_no_warm_up_at_all(self):
        result = services.daily_returns_for_instrument(self.aapl)

        self.assertEqual(result.padding, 0)
        self.assertIsNone(result.points[0].previous_close)

    def test_the_range_bounds_the_returned_dates(self):
        result = services.daily_returns_for_instrument(
            self.aapl, start_date=DAY2, end_date=DAY3
        )

        self.assertEqual([point.trading_date for point in result.points], [DAY2, DAY3])

    def test_the_whole_request_is_one_query(self):
        with self.assertNumQueries(1):
            services.daily_returns_for_instrument(
                self.aapl, start_date=DAY2, end_date=DAY5
            )

    def test_the_backend_does_not_change_the_answer(self):
        answers = {
            backend: services.daily_returns_for_instrument(self.aapl, backend=backend)
            for backend in services.DAILY_RETURN_BACKENDS
        }

        reference = answers["sql"].points
        for backend, result in answers.items():
            with self.subTest(backend=backend):
                self.assertEqual(result.points, reference)
                self.assertEqual(result.backend, backend)

    def test_an_unknown_backend_is_rejected_by_name(self):
        with self.assertRaises(ValueError) as caught:
            services.daily_returns_for_instrument(self.aapl, backend="prophet")

        self.assertIn("prophet", str(caught.exception))
        self.assertIn("sql", str(caught.exception))

    def test_undefined_values_are_counted_not_dropped(self):
        result = services.daily_returns_for_instrument(self.aapl)

        # The first day of the series, and nothing else.
        self.assertEqual(result.undefined, 1)
        self.assertEqual(len(result.points), 5)

    def test_an_instrument_with_one_price_has_no_return_and_no_error(self):
        single = create_instrument("SINGLE")
        add_price(single, DAY1, "10", "10", "10", "10", 5)

        result = services.daily_returns_for_instrument(single)

        self.assertEqual(len(result.points), 1)
        self.assertIsNone(result.points[0].value)
        self.assertEqual(result.undefined, 1)

    def test_an_instrument_without_prices_returns_an_empty_series(self):
        empty = create_instrument("EMPTY")

        result = services.daily_returns_for_instrument(empty)

        self.assertEqual(result.points, [])
        self.assertEqual(result.undefined, 0)
        self.assertEqual(result.symbol, "EMPTY")

    def test_a_zero_close_is_reported_as_undefined_and_the_count_reflects_it(self):
        zeroed = create_instrument("ZEROED")
        for offset, close in enumerate(["10", "0", "5"]):
            add_price(
                zeroed,
                DAY1 + timedelta(days=offset),
                close,
                close,
                close,
                close,
                1000,
            )

        result = services.daily_returns_for_instrument(zeroed)

        self.assertEqual(values(result.points), [None, "-1.00000000", None])
        self.assertEqual(result.undefined, 2)


class TradingDaysNotCalendarDaysTests(TestCase):
    """A gap is absent rows, not zero prices and not a shortened window."""

    def test_returns_span_the_gap_instead_of_reading_the_gap_as_a_day(self):
        instrument = create_instrument("GAPPY")
        # Monday, Tuesday, then *next* Monday: a five-day gap in the calendar.
        add_price(instrument, date(2024, 3, 11), "100", "100", "100", "100", 10)
        add_price(instrument, date(2024, 3, 12), "110", "110", "110", "110", 10)
        add_price(instrument, date(2024, 3, 18), "121", "121", "121", "121", 10)

        result = services.daily_returns_for_instrument(instrument)

        # Three observations, two returns: the gap is not a fourth observation
        # and does not produce a return of its own.
        self.assertEqual(len(result.points), 3)
        self.assertEqual(values(result.points), [None, "0.10000000", "0.10000000"])
        self.assertEqual(
            [point.trading_date for point in result.points],
            [date(2024, 3, 11), date(2024, 3, 12), date(2024, 3, 18)],
        )

    def test_a_range_bounded_by_calendar_dates_returns_only_trading_days(self):
        instrument = create_instrument("GAPPY")
        add_price(instrument, date(2024, 3, 11), "100", "100", "100", "100", 10)
        add_price(instrument, date(2024, 3, 18), "110", "110", "110", "110", 10)

        result = services.daily_returns_for_instrument(
            instrument, start_date=date(2024, 3, 11), end_date=date(2024, 3, 20)
        )

        self.assertEqual(
            [point.trading_date for point in result.points],
            [date(2024, 3, 11), date(2024, 3, 18)],
        )
