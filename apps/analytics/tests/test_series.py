"""The read boundary: what is read, in what order, and how warm-up rows work.

These tests are deliberately about *retrieval* rather than arithmetic. The two
properties that are easy to get quietly wrong -- reading only the columns a
metric needs, and looking back one row further than the range so the first answer
is not a hole -- are both invisible in a result set and expensive to discover in
production.
"""

from datetime import date
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from apps.marketdata.models import PriceRecord, TradingCalendar
from apps.marketdata.tests.helpers import (
    DAY1,
    DAY2,
    DAY3,
    DAY4,
    DAY5,
    SAMPLE_DATES,
    add_price,
    create_exchange,
    create_instrument,
    load_sample_series,
)

from ..metrics import quantize_price
from ..series import (
    PriceSeries,
    PriceSeriesError,
    load_price_series,
    missing_trading_dates,
)

AAPL_CLOSES = ["100", "102", "101", "103", "104"]


class PriceSeriesInvariantsTests(SimpleTestCase):
    """The container's own rules, with no database involved."""

    def test_repeated_dates_are_rejected_rather_than_deduplicated(self):
        series = PriceSeries(
            symbol="AAPL",
            dates=(DAY1, DAY2, DAY2),
            columns={"close": (Decimal("1"), Decimal("2"), Decimal("3"))},
        )

        with self.assertRaises(PriceSeriesError) as caught:
            series.assert_one_row_per_date()

        self.assertIn("strictly increasing", str(caught.exception))
        self.assertIn("AAPL", str(caught.exception))

    def test_out_of_order_dates_are_rejected(self):
        series = PriceSeries(
            symbol="AAPL",
            dates=(DAY2, DAY1),
            columns={"close": (Decimal("1"), Decimal("2"))},
        )

        with self.assertRaises(PriceSeriesError):
            series.assert_one_row_per_date()

    def test_a_single_row_series_is_legal(self):
        series = PriceSeries(
            symbol="AAPL", dates=(DAY1,), columns={"close": (Decimal("1"),)}
        )

        series.assert_one_row_per_date()
        self.assertEqual(len(series), 1)

    def test_in_range_drops_the_warm_up_rows_from_every_column(self):
        series = PriceSeries(
            symbol="AAPL",
            dates=(DAY1, DAY2, DAY3),
            columns={"close": (Decimal("1"), Decimal("2"), Decimal("3")), "volume": (1, 2, 3)},
            padding=2,
        )

        trimmed = series.in_range()

        self.assertEqual(trimmed.dates, (DAY3,))
        self.assertEqual(trimmed.closes, (Decimal("3"),))
        self.assertEqual(trimmed.column("volume"), (3,))
        self.assertEqual(trimmed.padding, 0)

    def test_a_series_without_padding_is_returned_unchanged(self):
        series = PriceSeries(
            symbol="AAPL", dates=(DAY1,), columns={"close": (Decimal("1"),)}
        )

        self.assertIs(series.in_range(), series)

    def test_asking_for_a_column_that_was_not_read_names_what_was(self):
        series = PriceSeries(
            symbol="AAPL", dates=(DAY1,), columns={"close": (Decimal("1"),)}
        )

        with self.assertRaises(KeyError) as caught:
            series.column("volume")

        self.assertIn("volume", str(caught.exception))
        self.assertIn("close", str(caught.exception))


class LoaderTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()
        self.aapl = self.instruments["AAPL"]

    # ----------------------------------------------------------------- #
    # Shape of the query
    # ----------------------------------------------------------------- #

    def test_the_series_is_read_in_one_query_in_date_order(self):
        with self.assertNumQueries(1):
            series = load_price_series(self.aapl)

        self.assertEqual(series.dates, tuple(SAMPLE_DATES))
        self.assertEqual(series.closes, tuple(Decimal(value) for value in AAPL_CLOSES))
        self.assertEqual(series.padding, 0)
        self.assertEqual(series.symbol, "AAPL")

    def test_only_the_columns_a_metric_reads_are_selected(self):
        with CaptureQueriesContext(connection) as captured:
            load_price_series(self.aapl, fields=("volume",))
            statements = [query["sql"] for query in captured]

        self.assertEqual(len(statements), 1)
        sql = statements[0]

        # The two columns that were asked for, and nothing else.
        self.assertIn("close", sql)
        self.assertIn("volume", sql)

        # Metadata no metric reads is not retrieved at all -- not selected and
        # then ignored, which would still cost the bytes and the parse.
        for unused in ("source", "ingestion_job_id", "created_at", "adjusted_close"):
            self.assertNotIn(unused, sql)

        # The instrument is not joined: the caller already holds the instance, so
        # the symbol does not need to come from the database again.
        self.assertNotIn("JOIN", sql.upper())
        self.assertNotIn("instruments_instrument", sql)

    def test_a_date_range_bounds_the_rows_and_both_bounds_are_inclusive(self):
        series = load_price_series(self.aapl, start_date=DAY2, end_date=DAY4)

        self.assertEqual(series.dates, (DAY2, DAY3, DAY4))

    def test_a_range_that_starts_after_the_series_is_empty_rather_than_an_error(self):
        series = load_price_series(self.aapl, start_date=date(2030, 1, 1))

        self.assertEqual(series.dates, ())
        self.assertEqual(series.padding, 0)

    # ----------------------------------------------------------------- #
    # Warm-up rows
    # ----------------------------------------------------------------- #

    def test_padding_reads_the_row_before_the_start_of_the_range(self):
        """The payoff: the first requested date has a predecessor to compare to."""
        with self.assertNumQueries(1):
            series = load_price_series(
                self.aapl, start_date=DAY2, end_date=DAY5, pad=1
            )

        self.assertEqual(series.dates, (DAY1, DAY2, DAY3, DAY4, DAY5))
        self.assertEqual(series.padding, 1)
        # What the metric sees, and what the caller is answered with.
        self.assertEqual(series.in_range().dates, (DAY2, DAY3, DAY4, DAY5))

    def test_padding_larger_than_the_history_still_returns_the_requested_rows(self):
        """The COALESCE fallback: a short history must not empty the response.

        With ``pad=10`` and only one row before the range, a naive
        ``trading_date >= <10th prior row>`` compares against NULL, which SQL
        treats as unknown -- and every requested row would disappear.
        """
        series = load_price_series(self.aapl, start_date=DAY2, end_date=DAY5, pad=10)

        # Every row there is before DAY2 is pulled in as warm-up...
        self.assertEqual(series.dates, (DAY1, DAY2, DAY3, DAY4, DAY5))
        self.assertEqual(series.padding, 1)
        # ...and none of the requested rows is lost.
        self.assertEqual(series.in_range().dates, (DAY2, DAY3, DAY4, DAY5))

    def test_padding_needs_a_start_date_to_have_any_meaning(self):
        series = load_price_series(self.aapl, pad=1, end_date=DAY5)

        self.assertEqual(series.dates, tuple(SAMPLE_DATES))
        self.assertEqual(series.padding, 0)

    def test_the_previous_close_column_is_the_previous_session(self):
        series = load_price_series(
            self.aapl, start_date=DAY2, end_date=DAY5, pad=1, with_previous_close=True
        )

        self.assertEqual(
            series.column("previous_close"),
            (None, Decimal("100"), Decimal("102"), Decimal("101"), Decimal("103")),
        )

    def test_the_window_does_not_read_across_instruments(self):
        series = load_price_series(
            self.instruments["GOOG"],
            start_date=DAY2,
            end_date=DAY3,
            pad=1,
            with_previous_close=True,
        )

        # GOOG's first row lags to nothing, not to AAPL's close: the window is
        # partitioned by instrument even though this query reads one of them.
        self.assertEqual(series.column("previous_close")[0], None)
        self.assertEqual(series.column("previous_close")[1], Decimal("50"))

    def test_the_moving_average_column_is_the_trailing_mean(self):
        series = load_price_series(
            self.aapl,
            start_date=DAY3,
            end_date=DAY5,
            pad=2,
            moving_average_window=3,
        )

        # (100 + 102 + 101) / 3, then (102 + 101 + 103) / 3, then
        # (101 + 103 + 104) / 3. Quantized, not compared raw: what the driver
        # hands back for an average has whatever scale the backend chose for it
        # (SQLite returned twelve places here), and normalising that is the
        # engine's job, not the loader's.
        self.assertEqual(
            [quantize_price(value) for value in series.column("moving_average")[2:]],
            [Decimal("101"), Decimal("102"), Decimal("102.66666667")],
        )
        self.assertEqual(series.padding, 2)

    def test_a_database_average_of_a_partial_frame_is_still_returned_as_data(self):
        """The loader does not hide it; the *metric* applies the completeness rule.

        The database's frame averages however many rows it has, so the first rows
        come back as partial means. Passing them through unchanged is what lets
        the metric decide -- and what makes a supplied mean comparable with a
        computed one.
        """
        series = load_price_series(self.aapl, moving_average_window=7)

        self.assertEqual(quantize_price(series.column("moving_average")[0]), Decimal("100"))
        self.assertEqual(quantize_price(series.column("moving_average")[1]), Decimal("101"))

    def test_both_window_columns_can_be_read_together_in_one_query(self):
        with self.assertNumQueries(1):
            series = load_price_series(
                self.aapl,
                start_date=DAY2,
                pad=1,
                with_previous_close=True,
                moving_average_window=2,
            )

        self.assertEqual(series.column("previous_close")[1], Decimal("100"))
        self.assertEqual(series.column("moving_average")[1], Decimal("101"))

    def test_a_moving_average_window_below_one_is_rejected(self):
        with self.assertRaises(ValueError):
            load_price_series(self.aapl, moving_average_window=0)

    def test_the_running_max_column_is_the_highest_close_so_far(self):
        series = load_price_series(
            self.aapl, start_date=DAY1, end_date=DAY5, with_running_max=True
        )

        # 100, then max(100, 102), then max(..., 101) = 102, then 103, then 104.
        self.assertEqual(
            series.column("running_max"),
            (
                Decimal("100"),
                Decimal("102"),
                Decimal("102"),
                Decimal("103"),
                Decimal("104"),
            ),
        )

    def test_the_running_max_starts_at_the_ranges_first_row(self):
        """The frame is unbounded *preceding within the rows returned*, so the
        date range is what anchors it -- the property the drawdown's ``pad=0``
        depends on. DAY2's 102 is the highest close in the table, and a range
        starting on DAY3 must not see it.
        """
        series = load_price_series(
            self.aapl, start_date=DAY3, end_date=DAY5, with_running_max=True
        )

        self.assertEqual(
            series.column("running_max"),
            (Decimal("101"), Decimal("103"), Decimal("104")),
        )

    def test_all_three_window_columns_can_be_read_together_in_one_query(self):
        with self.assertNumQueries(1):
            series = load_price_series(
                self.aapl,
                start_date=DAY2,
                pad=1,
                with_previous_close=True,
                moving_average_window=2,
                with_running_max=True,
            )

        self.assertEqual(series.column("previous_close")[1], Decimal("100"))
        self.assertEqual(series.column("moving_average")[1], Decimal("101"))
        self.assertEqual(series.column("running_max")[1], Decimal("102"))

    def test_the_running_extremes_columns_come_back_from_one_query(self):
        with self.assertNumQueries(1):
            series = load_price_series(
                self.aapl, start_date=DAY1, end_date=DAY5, with_running_extremes=True
            )

        # 100, 102, 101, 103, 104: the max only moves up, the min never moves.
        self.assertEqual(
            series.column("running_max"),
            (
                Decimal("100"),
                Decimal("102"),
                Decimal("102"),
                Decimal("103"),
                Decimal("104"),
            ),
        )
        self.assertEqual(series.column("running_min"), (Decimal("100"),) * 5)

    def test_a_two_direction_series_anchors_both_extremes_at_the_range_start(self):
        """The minimum is anchored by the same date bound as the maximum, so a low
        that happened before the range is invisible -- the property the extreme
        metric's ``pad=0`` depends on."""
        series = load_price_series(
            self.aapl, start_date=DAY2, end_date=DAY5, with_running_extremes=True
        )

        self.assertEqual(
            series.column("running_min"),
            (Decimal("102"), Decimal("101"), Decimal("101"), Decimal("101")),
        )

    # ----------------------------------------------------------------- #
    # Arguments
    # ----------------------------------------------------------------- #

    def test_a_field_that_is_not_a_price_column_is_rejected_by_name(self):
        with self.assertRaises(ValueError) as caught:
            load_price_series(self.aapl, fields=("symbol",))

        self.assertIn("symbol", str(caught.exception))
        self.assertIn("volume", str(caught.exception))

    def test_a_negative_pad_is_rejected(self):
        with self.assertRaises(ValueError):
            load_price_series(self.aapl, pad=-1)

    def test_an_inverted_range_is_rejected(self):
        with self.assertRaises(ValueError):
            load_price_series(self.aapl, start_date=DAY5, end_date=DAY1)

    # ----------------------------------------------------------------- #
    # Why duplicate dates cannot reach the loader
    # ----------------------------------------------------------------- #

    def test_the_database_refuses_a_second_row_for_one_instrument_and_day(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            add_price(self.aapl, DAY1, "1", "1", "1", "1", 1)

        self.assertEqual(
            PriceRecord.objects.filter(instrument=self.aapl, trading_date=DAY1).count(),
            1,
        )


class MissingTradingDatesTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()

    def test_sessions_with_no_price_are_reported(self):
        gappy = create_instrument("GAPPY")
        add_price(gappy, DAY1, "100", "100", "100", "100", 1000)
        add_price(gappy, DAY2, "100", "100", "100", "100", 1000)
        add_price(gappy, DAY5, "100", "100", "100", "100", 1000)

        self.assertEqual(
            missing_trading_dates(gappy, DAY1, DAY5), [DAY3, DAY4]
        )

    def test_a_fully_loaded_series_reports_no_gaps(self):
        self.assertEqual(missing_trading_dates(self.instruments["AAPL"], DAY1, DAY5), [])

    def test_a_calendar_row_belonging_to_another_exchange_is_not_a_session_here(self):
        other_exchange = create_exchange(code="LSE", name="London")
        nasdaq = self.instruments["AAPL"].exchange

        TradingCalendar.objects.create(date=date(2024, 4, 1), exchange=other_exchange)
        TradingCalendar.objects.create(date=date(2024, 4, 2), exchange=nasdaq)
        TradingCalendar.objects.create(date=date(2024, 4, 3), exchange=None)

        # A price on 04-02, nothing on the other two days.
        add_price(self.instruments["AAPL"], date(2024, 4, 2), "1", "1", "1", "1", 1)

        self.assertEqual(
            missing_trading_dates(self.instruments["AAPL"], date(2024, 4, 1), date(2024, 4, 3)),
            # 04-01 is another market's session, so it is not a gap in this
            # instrument's history; 04-03 is unscoped, so it applies here.
            [date(2024, 4, 3)],
        )
