"""Percentage price change over a selected period: one number, and the sessions it spans.

The standard sample series, closes 100, 102, 101, 103, 104 on consecutive sessions:

=========================  -------  --------  ------------  ------------
selected period            base     end       ``change``    ``percent_change``
=========================  -------  --------  ------------  ------------
2024-03-11 .. 2024-03-15   100      104       ``0.04000000``     ``4.00000000``
2024-03-12 .. 2024-03-15   102      104       ``0.01960784``     ``1.96078400``
2024-03-13 .. 2024-03-15   101      104       ``0.02970297``     ``2.97029700``
2024-03-14 .. 2024-03-15   103      104       ``0.00970874``     ``0.97087400``
2024-03-13 .. 2024-03-13   101      101       ``0.00000000``     ``0.00000000``
=========================  -------  --------  ------------  ------------

``change`` is ``(end - base) / base``: ``2 / 102`` is ``0.0196078431...``, rounded
half up at the eighth place. ``percent_change`` is the ratio times 100 -- derived
from the reported ratio rather than recomputed from the prices, so the two can never
disagree and a reader can check one against the other.

A period whose bounds are not trading days is snapped to the sessions inside it, and
the sessions actually used are reported back: asking for Saturday 2024-03-09 through
Tuesday 2024-03-12 gives a base of Monday 2024-03-11 and an end of Tuesday
2024-03-12, over two observations.

The cross-metric check: a period that *is* the requested range gives the number
:func:`apps.analytics.metrics.cumulative_returns` ends on, because
``cumulative_returns(total) = close(last) / close(first) - 1`` by definition. The two
are asserted equal rather than assumed equal, which is the only thing that makes the
second computation worth having.
"""

from datetime import date
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
    SelectedPeriod,
    format_decimal,
    period_change,
    period_change_pandas,
)

DATES = [DAY1, DAY2, DAY3, DAY4, DAY5]
CLOSES = [Decimal(value) for value in ("100", "102", "101", "103", "104")]


def rendered(change):
    return format_decimal(change.change), format_decimal(change.percent_change)


class PeriodChangeUnitTests(SimpleTestCase):
    def test_the_whole_range(self):
        change = period_change(DATES, CLOSES)

        self.assertEqual(change.base_date, DAY1)
        self.assertEqual(change.base_close, Decimal("100"))
        self.assertEqual(change.end_date, DAY5)
        self.assertEqual(change.end_close, Decimal("104"))
        self.assertEqual(rendered(change), ("0.04000000", "4.00000000"))
        self.assertEqual(change.observations, 5)
        self.assertIsNone(change.label)

    def test_a_period_inside_the_range(self):
        change = period_change(DATES, CLOSES, start_date=DAY2, end_date=DAY5)

        self.assertEqual(change.base_date, DAY2)
        self.assertEqual(change.end_date, DAY5)
        # 2 / 102 = 0.0196078431...
        self.assertEqual(rendered(change), ("0.01960784", "1.96078400"))
        self.assertEqual(change.observations, 4)

    def test_a_one_session_period_is_a_change_of_zero(self):
        change = period_change(DATES, CLOSES, start_date=DAY3, end_date=DAY3)

        self.assertEqual(change.base_date, DAY3)
        self.assertEqual(change.end_date, DAY3)
        self.assertEqual(rendered(change), ("0.00000000", "0.00000000"))
        self.assertEqual(change.observations, 1)

    def test_a_loss_is_signed(self):
        change = period_change(DATES[:2], [Decimal("100"), Decimal("90")])

        self.assertEqual(rendered(change), ("-0.10000000", "-10.00000000"))

    def test_the_percent_is_exactly_the_ratio_times_one_hundred(self):
        """Derived from the reported ratio rather than from the prices, so the two
        fields can never tell a reader two different things."""
        for start, end in (
            (None, None),
            (DAY2, None),
            (DAY3, DAY5),
            (DAY4, DAY4),
        ):
            with self.subTest(start=start, end=end):
                change = period_change(DATES, CLOSES, start_date=start, end_date=end)

                self.assertEqual(change.percent_change, change.change * 100)

    def test_a_bound_that_is_not_a_session_snaps_to_the_sessions_inside_it(self):
        """Saturday through Tuesday: the base is Monday's session and the end is
        Tuesday's, and both are reported so the caller can see the snap."""
        saturday = date(2024, 3, 9)
        tuesday = date(2024, 3, 12)
        self.assertEqual(saturday.weekday(), 5)

        change = period_change(DATES, CLOSES, start_date=saturday, end_date=tuesday)

        self.assertEqual(change.base_date, DAY1)
        self.assertEqual(change.end_date, DAY2)
        self.assertEqual(rendered(change), ("0.02000000", "2.00000000"))
        self.assertEqual(change.observations, 2)

    def test_a_period_with_no_observation_in_it_has_no_change(self):
        change = period_change(
            DATES, CLOSES, start_date=date(2024, 3, 16), end_date=date(2024, 3, 18)
        )

        self.assertEqual(change.change, None)
        self.assertEqual(change.percent_change, None)
        self.assertEqual(change.observations, 0)
        self.assertIsNone(change.base_date)
        self.assertIsNone(change.end_date)

    def test_a_period_narrower_than_a_gap_between_sessions_has_no_change(self):
        """The base snaps forward to the session after the period and the end snaps
        back to the one before it -- which cross, so there is no session *inside* the
        period to measure."""
        dates = [DAY1, DAY5]
        closes = [Decimal("100"), Decimal("104")]

        change = period_change(dates, closes, start_date=DAY2, end_date=DAY4)

        self.assertEqual(change.change, None)
        self.assertEqual(change.observations, 0)

    def test_a_base_after_the_last_session_has_no_change(self):
        change = period_change(DATES, CLOSES, start_date=date(2025, 1, 1))

        self.assertEqual(change.change, None)
        self.assertEqual(change.observations, 0)

    def test_an_end_before_the_first_session_has_no_change(self):
        change = period_change(DATES, CLOSES, end_date=date(2020, 1, 1))

        self.assertEqual(change.change, None)
        self.assertEqual(change.observations, 0)

    def test_a_base_of_zero_has_no_ratio_but_the_sessions_are_still_counted(self):
        change = period_change(DATES[:2], [Decimal("0"), Decimal("5")])

        self.assertEqual(change.change, None)
        self.assertEqual(change.percent_change, None)
        self.assertEqual(change.base_close, Decimal("0"))
        self.assertEqual(change.end_close, Decimal("5"))
        self.assertEqual(change.observations, 2)

    def test_the_label_is_carried_through(self):
        change = period_change(DATES, CLOSES, label="this quarter")

        self.assertEqual(change.label, "this quarter")

    def test_the_dates_are_the_observations_used_not_the_bounds_requested(self):
        """DAY2 through the first session of the next month: the end reported back is
        the last session available, which is what a report has to quote."""
        change = period_change(DATES, CLOSES, start_date=DAY2, end_date=date(2024, 4, 1))

        self.assertEqual(change.end_date, DAY5)

    def test_a_reversed_period_is_rejected(self):
        with self.assertRaises(ValueError):
            period_change(DATES, CLOSES, start_date=DAY4, end_date=DAY2)

    def test_mismatched_lengths_are_rejected(self):
        with self.assertRaises(ValueError):
            period_change(DATES, CLOSES[:2])

    def test_an_empty_series_is_an_empty_answer(self):
        change = period_change([], [])

        self.assertEqual(change.change, None)
        self.assertEqual(change.observations, 0)

    def test_the_pandas_backend_agrees_on_every_case(self):
        cases = (
            {},
            {"start_date": DAY2},
            {"start_date": DAY3, "end_date": DAY4},
            {"start_date": DAY3, "end_date": DAY3},
            {"start_date": date(2024, 3, 9), "end_date": date(2024, 3, 12)},
            {"start_date": date(2024, 3, 16), "end_date": date(2024, 3, 18)},
            {"start_date": date(2025, 1, 1)},
            {"end_date": date(2020, 1, 1)},
            {"label": "labelled"},
        )

        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(
                    period_change_pandas(DATES, CLOSES, **case),
                    period_change(DATES, CLOSES, **case),
                )

    def test_the_pandas_backend_agrees_when_the_period_is_narrower_than_a_gap(self):
        dates = [DAY1, DAY5]
        closes = [Decimal("100"), Decimal("104")]

        self.assertEqual(
            period_change_pandas(dates, closes, start_date=DAY2, end_date=DAY4),
            period_change(dates, closes, start_date=DAY2, end_date=DAY4),
        )

    def test_the_pandas_backend_agrees_on_an_empty_series(self):
        self.assertEqual(period_change_pandas([], []), period_change([], []))

    def test_the_pandas_backend_rejects_a_reversed_period_too(self):
        with self.assertRaises(ValueError):
            period_change_pandas(DATES, CLOSES, start_date=DAY4, end_date=DAY2)


class PeriodChangeServiceTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()
        self.aapl = self.instruments["AAPL"]

    def test_the_whole_range_is_the_one_number(self):
        result = services.percentage_change_for_instrument(self.aapl)

        self.assertEqual(len(result.periods), 1)
        self.assertEqual(rendered(result.periods[0]), ("0.04000000", "4.00000000"))
        self.assertEqual(result.periods[0].observations, 5)
        self.assertEqual(result.padding, 0)
        self.assertEqual(result.undefined, 0)

    def test_a_period_that_is_the_range_agrees_with_the_cumulative_return(self):
        """The same fact seen twice: the end of the compounded series is the change
        over the period, computed here from the two prices instead of by chaining."""
        for start in (None, DAY2, DAY3):
            with self.subTest(start=start):
                change = services.percentage_change_for_instrument(
                    self.aapl, start_date=start
                )
                cumulative = services.cumulative_returns_for_instrument(
                    self.aapl, start_date=start
                )

                self.assertEqual(change.periods[0].change, cumulative.total)

    def test_a_selected_period_need_not_be_the_whole_range(self):
        result = services.percentage_change_for_instrument(
            self.aapl, start_date=DAY2, end_date=DAY4
        )

        # 103 / 102 - 1
        self.assertEqual(rendered(result.periods[0]), ("0.00980392", "0.98039200"))
        self.assertEqual(result.periods[0].base_date, DAY2)
        self.assertEqual(result.periods[0].end_date, DAY4)

    def test_the_span_is_read_but_the_change_uses_only_its_endpoints(self):
        """DAY2's 102 is the base, DAY4's 103 the end, and DAY3's 101 does not enter
        the answer -- though it is an observation of the period."""
        result = services.percentage_change_for_instrument(
            self.aapl, start_date=DAY2, end_date=DAY4
        )
        period = result.periods[0]

        self.assertEqual((period.base_close, period.end_close), (Decimal("102"), Decimal("103")))
        self.assertEqual(period.observations, 3)

    def test_one_period_is_one_query(self):
        with self.assertNumQueries(1):
            services.percentage_change_for_instrument(self.aapl)

    def test_several_periods_are_still_one_query(self):
        periods = (
            SelectedPeriod(label="first two", start_date=DAY1, end_date=DAY2),
            SelectedPeriod(label="last two", start_date=DAY4, end_date=DAY5),
            SelectedPeriod(label="all five", start_date=DAY1, end_date=DAY5),
        )

        with self.assertNumQueries(1):
            result = services.percentage_change_for_instrument(
                self.aapl, periods=periods
            )

        self.assertEqual([period.label for period in result.periods], ["first two", "last two", "all five"])
        self.assertEqual(
            [format_decimal(period.change) for period in result.periods],
            ["0.02000000", "0.00970874", "0.04000000"],
        )

    def test_several_periods_are_read_over_their_union(self):
        periods = (
            SelectedPeriod(label="early", start_date=DAY1, end_date=DAY2),
            SelectedPeriod(label="late", start_date=DAY4, end_date=DAY5),
        )

        result = services.percentage_change_for_instrument(self.aapl, periods=periods)

        self.assertEqual(result.start_date, DAY1)
        self.assertEqual(result.end_date, DAY5)
        self.assertEqual(result.padding, 0)

    def test_a_period_with_an_open_bound_opens_the_read_too(self):
        periods = (
            SelectedPeriod(label="to the third", end_date=DAY3),
            SelectedPeriod(label="from the fourth", start_date=DAY4),
        )

        result = services.percentage_change_for_instrument(self.aapl, periods=periods)

        self.assertIsNone(result.start_date)
        self.assertIsNone(result.end_date)
        self.assertEqual(
            [format_decimal(period.change) for period in result.periods],
            ["0.01000000", "0.00970874"],
        )

    def test_both_forms_together_are_rejected(self):
        with self.assertRaises(ValueError):
            services.percentage_change_for_instrument(
                self.aapl,
                start_date=DAY1,
                periods=(SelectedPeriod(label="x", start_date=DAY1),),
            )

    def test_an_empty_selection_asks_for_nothing_and_reads_nothing(self):
        with self.assertNumQueries(0):
            result = services.percentage_change_for_instrument(self.aapl, periods=())

        self.assertEqual(result.periods, ())
        self.assertEqual(result.undefined, 0)

    def test_an_instrument_without_prices_answers_one_empty_period(self):
        empty = create_instrument("EMPTY")

        result = services.percentage_change_for_instrument(empty)

        self.assertEqual(len(result.periods), 1)
        self.assertEqual(result.periods[0].change, None)
        self.assertEqual(result.undefined, 1)

    def test_the_backends_agree_on_the_sample_series(self):
        for symbol in ("AAPL", "MSFT", "GOOG"):
            with self.subTest(symbol=symbol):
                instrument = self.instruments[symbol]

                python = services.percentage_change_for_instrument(
                    instrument, backend="python"
                )
                pandas = services.percentage_change_for_instrument(
                    instrument, backend="pandas"
                )

                self.assertEqual(python.periods, pandas.periods)

    def test_the_backends_agree_on_several_periods(self):
        periods = (
            SelectedPeriod(label="a", start_date=DAY1, end_date=DAY3),
            SelectedPeriod(label="b", start_date=DAY2),
        )

        python = services.percentage_change_for_instrument(
            self.aapl, periods=periods, backend="python"
        )
        pandas = services.percentage_change_for_instrument(
            self.aapl, periods=periods, backend="pandas"
        )

        self.assertEqual(python.periods, pandas.periods)

    def test_a_period_of_one_session_over_stored_data_is_zero(self):
        result = services.percentage_change_for_instrument(
            self.aapl, start_date=DAY3, end_date=DAY3
        )

        self.assertEqual(rendered(result.periods[0]), ("0.00000000", "0.00000000"))

    def test_a_gapped_instrument_counts_observations_not_calendar_days(self):
        gapped = create_instrument("GAPPED")
        for day, close in ((DAY1, "10"), (DAY5, "12")):
            add_price(gapped, day, close, close, close, close, 1)

        result = services.percentage_change_for_instrument(
            gapped, start_date=DAY1, end_date=DAY5
        )
        period = result.periods[0]

        self.assertEqual(format_decimal(period.change), "0.20000000")
        self.assertEqual(period.observations, 2)
        self.assertEqual((period.end_date - period.base_date).days, 4)

    def test_the_sql_backend_is_not_offered(self):
        """A period change is two rows and a division, not a window; the read
        boundary returns a series, so there is no SQL member to default to."""
        with self.assertRaises(ValueError) as caught:
            services.percentage_change_for_instrument(self.aapl, backend="sql")

        self.assertIn("sql", str(caught.exception))
        self.assertIn("python", str(caught.exception))

    def test_an_unknown_backend_is_rejected_by_name(self):
        with self.assertRaises(ValueError) as caught:
            services.percentage_change_for_instrument(self.aapl, backend="prophet")

        self.assertIn("prophet", str(caught.exception))
