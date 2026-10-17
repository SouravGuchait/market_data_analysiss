"""Rankings across instruments: the arithmetic, and the entry point that uses it.

Two layers, mirroring the code: :func:`rank_measures` on its own (no database at
all -- a rank is a property of the values), and the entry point that reads the
cross-section through :func:`load_price_cross_section` and joins the two. The
cross-section read and ``PriceSeries.tail`` have their own tests in
``test_series.py``, which is where the read boundary is tested.

The sample is deliberately a ranking fixture: the returns 0.04, 0.05 and 0.08 are
all distinct, as are the volume totals 5000, 2500 and 10000, so the expected order
of every ranking is a hand-checked number rather than whatever the code produced.
"""

from datetime import date
from decimal import Decimal

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from apps.marketdata.tests.helpers import (
    DAY1,
    DAY2,
    DAY3,
    DAY4,
    DAY5,
    EXPECTED_RETURN,
    EXPECTED_RETURN_ORDER,
    add_price,
    create_instrument,
    load_sample_series,
)

from ..metrics import (
    RANKING_MEASURES,
    MeasurePoint,
    rank_measures,
)
from ..services import (
    RANKING_BACKENDS,
    MetricResult,
    Ranking,
    percentage_change_for_instrument,
    ranking_for_instruments,
    volume_summaries_for_instrument,
)


def point(symbol, value, *, instrument_id=0, observations=5, first=DAY1, last=DAY5):
    """A measure point, with the defaults the tests do not care about."""
    return MeasurePoint(
        instrument_id=instrument_id,
        symbol=symbol,
        value=None if value is None else Decimal(value),
        observations=observations,
        first_date=first,
        last_date=last,
    )


class RankArithmeticTests(SimpleTestCase):
    """``rank_measures``: ordering, competition ranks, ties, and truncation."""

    def test_descending_puts_the_largest_measure_first(self):
        rows = rank_measures(
            [point("A", "0.04"), point("B", "0.05"), point("C", "0.08")]
        )

        self.assertEqual([row.symbol for row in rows], ["C", "B", "A"])
        self.assertEqual([row.rank for row in rows], [1, 2, 3])

    def test_ascending_puts_the_smallest_measure_first(self):
        rows = rank_measures(
            [point("A", "0.04"), point("B", "0.05"), point("C", "0.08")],
            descending=False,
        )

        self.assertEqual([row.symbol for row in rows], ["A", "B", "C"])
        self.assertEqual([row.rank for row in rows], [1, 2, 3])

    def test_equal_measures_share_a_rank_and_the_next_one_skips(self):
        rows = rank_measures(
            [
                point("A", "0.08"),
                point("B", "0.05"),
                point("C", "0.05"),
                point("D", "0.01"),
            ]
        )

        # Competition ranking: 1, 2, 2, 4 -- the two tied instruments consume the
        # numbers 2 and 3 and the next distinct value takes 4.
        self.assertEqual([row.rank for row in rows], [1, 2, 2, 4])
        self.assertEqual([row.symbol for row in rows], ["A", "B", "C", "D"])

    def test_rank_is_one_more_than_the_number_strictly_better(self):
        values = {
            "A": "0.10",
            "B": "-0.02",
            "C": "0.10",
            "D": "0.00",
            "E": "0.10",
        }
        points = [point(symbol, value) for symbol, value in values.items()]

        rows = {row.symbol: row.rank for row in rank_measures(points)}

        for symbol, value in values.items():
            strictly_better = sum(1 for other in points if other.value > Decimal(value))
            self.assertEqual(rows[symbol], 1 + strictly_better, symbol)

    def test_the_order_does_not_depend_on_the_order_the_points_arrived_in(self):
        symbols = ["AAPL", "MSFT", "GOOG", "TSLA"]
        values = ["0.04", "0.05", "0.08", "0.01"]
        forward = rank_measures([point(s, v) for s, v in zip(symbols, values)])
        backward = rank_measures(
            [point(s, v) for s, v in zip(reversed(symbols), reversed(values))]
        )

        self.assertEqual(
            [(row.rank, row.symbol) for row in forward],
            [(row.rank, row.symbol) for row in backward],
        )

    def test_ties_are_broken_by_symbol(self):
        rows = rank_measures([point("MSFT", "0.05"), point("AAPL", "0.05")])

        self.assertEqual([row.symbol for row in rows], ["AAPL", "MSFT"])
        self.assertEqual([row.rank for row in rows], [1, 1])

    def test_a_point_that_cannot_be_measured_is_ranked_none_and_listed_last(self):
        rows = rank_measures(
            [point("A", "0.04"), point("B", None), point("C", "0.08"), point("D", None)]
        )

        self.assertEqual([row.symbol for row in rows], ["C", "A", "B", "D"])
        self.assertEqual([row.rank for row in rows], [1, 2, None, None])
        self.assertEqual(rows[2].value, None)

    def test_an_unmeasurable_point_comes_back_rather_than_being_dropped(self):
        rows = rank_measures([point("A", None)])

        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].rank)
        self.assertEqual(rows[0].observations, 5)

    def test_limit_truncates_without_renumbering(self):
        points = [point(symbol, value) for symbol, value in (
            ("A", "0.08"),
            ("B", "0.05"),
            ("C", "0.04"),
            ("D", "0.01"),
        )]

        rows = rank_measures(points, limit=2)

        # The third placed instrument is still third; a page of a ranking shows
        # absolute places, not places within the page.
        self.assertEqual([(row.rank, row.symbol) for row in rows], [(1, "A"), (2, "B")])

    def test_a_limit_larger_than_the_ranking_returns_everything(self):
        rows = rank_measures([point("A", "0.01")], limit=10)

        self.assertEqual(len(rows), 1)

    def test_a_limit_of_zero_returns_nothing(self):
        self.assertEqual(rank_measures([point("A", "0.01")], limit=0), [])

    def test_a_negative_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            rank_measures([point("A", "0.01")], limit=-1)

    def test_an_empty_ranking_is_empty(self):
        self.assertEqual(rank_measures([]), [])

    def test_every_instrument_can_be_unmeasurable(self):
        rows = rank_measures([point("B", None), point("A", None)])

        self.assertEqual([row.symbol for row in rows], ["A", "B"])
        self.assertEqual([row.rank for row in rows], [None, None])

    def test_the_inputs_of_the_measure_come_back_on_the_row(self):
        rows = rank_measures(
            [point("A", "0.04", instrument_id=7, observations=3, first=DAY2, last=DAY4)]
        )

        self.assertEqual(rows[0].instrument_id, 7)
        self.assertEqual(rows[0].observations, 3)
        self.assertEqual(rows[0].first_date, DAY2)
        self.assertEqual(rows[0].last_date, DAY4)
        self.assertEqual(rows[0].value, Decimal("0.04"))

    def test_ranking_does_not_mutate_the_points_it_is_given(self):
        points = [point("A", "0.04"), point("B", "0.08")]

        rank_measures(points)

        self.assertEqual([one.symbol for one in points], ["A", "B"])


class RankingServiceTests(TestCase):
    """The entry point, against the sample: 0.04 / 0.05 / 0.08 and 5000/2500/10000."""

    def setUp(self):
        self.instruments = load_sample_series()

    def test_return_ranking_is_the_expected_order(self):
        result = ranking_for_instruments(measure="return")

        self.assertEqual(list(result.symbols), EXPECTED_RETURN_ORDER)
        for row in result.rows:
            self.assertEqual(
                row.value, Decimal(EXPECTED_RETURN[row.symbol]), row.symbol
            )
            self.assertEqual(row.observations, 5)

    def test_return_ranking_ranks_from_one(self):
        result = ranking_for_instruments(measure="return")

        self.assertEqual([row.rank for row in result.rows], [1, 2, 3])
        self.assertEqual(result.ranked, 3)
        self.assertEqual(result.unranked, 0)
        self.assertEqual(result.universe, 3)

    def test_volume_ranking_is_the_expected_order(self):
        result = ranking_for_instruments(measure="volume")

        # AAPL 1000, MSFT 500, GOOG 2000 per session, over five sessions.
        self.assertEqual(list(result.symbols), ["GOOG", "AAPL", "MSFT"])
        self.assertEqual(
            [row.value for row in result.rows],
            [Decimal("10000"), Decimal("5000"), Decimal("2500")],
        )

    def test_the_volume_measure_is_the_volume_summary_total(self):
        result = ranking_for_instruments(measure="volume")

        for row in result.rows:
            summary = volume_summaries_for_instrument(
                self.instruments[row.symbol]
            ).summary
            self.assertEqual(row.value, summary.total_volume, row.symbol)

    def test_the_return_measure_is_the_percentage_change_over_the_same_range(self):
        result = ranking_for_instruments(
            measure="return", start_date=DAY2, end_date=DAY4
        )

        for row in result.rows:
            change = percentage_change_for_instrument(
                self.instruments[row.symbol], start_date=DAY2, end_date=DAY4
            ).periods[0]
            self.assertEqual(row.value, change.change, row.symbol)

    def test_every_backend_agrees_row_for_row_for_the_return(self):
        self.assert_backends_agree("return")

    def test_every_backend_agrees_row_for_row_for_the_volume(self):
        self.assert_backends_agree("volume")

    def assert_backends_agree(self, measure):
        results = [
            ranking_for_instruments(measure=measure, backend=backend)
            for backend in RANKING_BACKENDS
        ]

        first = results[0]
        for other in results[1:]:
            self.assertEqual(self.places(first), self.places(other), other.backend)

    @staticmethod
    def places(result):
        """The comparable part of a ranking: place, symbol, value, sessions."""
        return [
            (row.rank, row.symbol, row.value, row.observations) for row in result.rows
        ]

    def test_a_ranking_is_one_query(self):
        for backend in RANKING_BACKENDS:
            captured = CaptureQueriesContext(connection)
            with self.subTest(backend=backend), captured:
                ranking_for_instruments(measure="volume", backend=backend)

            self.assertEqual(len(captured), 1)

    def test_the_backends_are_the_two_without_a_sql_member(self):
        self.assertEqual(RANKING_BACKENDS, ("python", "pandas"))

    def test_descending_false_reports_the_other_end(self):
        result = ranking_for_instruments(measure="return", descending=False)

        self.assertEqual(list(result.symbols), list(reversed(EXPECTED_RETURN_ORDER)))
        self.assertEqual([row.rank for row in result.rows], [1, 2, 3])

    def test_the_measure_and_the_window_come_back_on_the_result(self):
        result = ranking_for_instruments(
            measure="volume",
            lookback=2,
            start_date=DAY1,
            end_date=DAY5,
            backend="pandas",
        )

        self.assertEqual(result.measure, "volume")
        self.assertEqual(result.lookback, 2)
        self.assertEqual(result.start_date, DAY1)
        self.assertEqual(result.end_date, DAY5)
        self.assertEqual(result.backend, "pandas")
        self.assertTrue(result.descending)

    def test_the_lookback_windows_each_instrument_over_its_newest_sessions(self):
        result = ranking_for_instruments(measure="return", lookback=2)

        # The newest two sessions: AAPL 103 -> 104, MSFT 205 -> 210, GOOG 53 -> 54.
        values = {row.symbol: row.value for row in result.rows}
        self.assertEqual(values["AAPL"], Decimal("0.00970874"))
        self.assertEqual(values["MSFT"], Decimal("0.02439024"))
        self.assertEqual(values["GOOG"], Decimal("0.01886792"))
        self.assertEqual(list(result.symbols), ["MSFT", "GOOG", "AAPL"])

    def test_the_lookback_windows_the_volume_total_too(self):
        result = ranking_for_instruments(measure="volume", lookback=2)

        values = {row.symbol: row.value for row in result.rows}
        self.assertEqual(
            values,
            {
                "AAPL": Decimal("2000"),
                "GOOG": Decimal("4000"),
                "MSFT": Decimal("1000"),
            },
        )

    def test_a_window_shorter_than_the_lookback_is_unranked(self):
        result = ranking_for_instruments(measure="return", lookback=10)

        self.assertEqual(result.universe, 3)
        self.assertEqual(result.ranked, 0)
        self.assertEqual(result.unranked, 3)
        for row in result.rows:
            self.assertIsNone(row.value)
            self.assertIsNone(row.rank)
            self.assertEqual(row.observations, 5)

    def test_a_short_window_is_unranked_for_the_volume_total_too(self):
        result = ranking_for_instruments(measure="volume", lookback=6)

        self.assertEqual(result.ranked, 0)
        self.assertEqual([row.symbol for row in result.rows], ["AAPL", "GOOG", "MSFT"])

    def test_a_one_observation_window_is_a_change_of_zero(self):
        # Metric 8's rule, unchanged: one session is a period with a change of 0.
        # ``observations`` on the row is what lets a reader see how narrow it was.
        result = ranking_for_instruments(measure="return", lookback=1)

        self.assertEqual(result.universe, 3)
        self.assertEqual(result.ranked, 3)
        for row in result.rows:
            self.assertEqual(row.value, Decimal("0"))
            self.assertEqual(row.observations, 1)

    def test_the_dates_bound_which_observations_are_eligible(self):
        result = ranking_for_instruments(
            measure="return", start_date=DAY2, end_date=DAY4
        )

        for row in result.rows:
            self.assertEqual(row.first_date, DAY2)
            self.assertEqual(row.last_date, DAY4)
            self.assertEqual(row.observations, 3)

        # AAPL 102 -> 103, MSFT 190 -> 205, GOOG 51 -> 53.
        values = {row.symbol: row.value for row in result.rows}
        self.assertEqual(values["AAPL"], Decimal("0.00980392"))
        self.assertEqual(values["MSFT"], Decimal("0.07894737"))
        self.assertEqual(values["GOOG"], Decimal("0.03921569"))

    def test_an_instrument_filter_limits_the_universe(self):
        result = ranking_for_instruments(
            measure="return", instrument_ids=[self.instruments["GOOG"].pk]
        )

        self.assertEqual(list(result.symbols), ["GOOG"])
        self.assertEqual(result.universe, 1)

    def test_an_unknown_instrument_id_is_simply_not_in_the_universe(self):
        result = ranking_for_instruments(measure="return", instrument_ids=[10**9])

        self.assertEqual(result.universe, 0)
        self.assertEqual(result.rows, ())

    def test_no_prices_at_all_is_an_empty_ranking(self):
        result = ranking_for_instruments(
            measure="return", start_date=date(2020, 1, 1), end_date=date(2020, 1, 2)
        )

        self.assertEqual(result.universe, 0)
        self.assertEqual(result.ranked, 0)
        self.assertEqual(result.rows, ())

    def test_an_instrument_with_no_prices_in_the_range_is_not_in_the_universe(self):
        create_instrument("TSLA")

        result = ranking_for_instruments(measure="return")

        self.assertEqual(result.universe, 3)
        self.assertNotIn("TSLA", result.symbols)

    def test_a_single_observation_window_is_listed_with_its_count(self):
        newer = create_instrument("NEW")
        add_price(newer, DAY5, "10", "10", "10", "10", 10)

        result = ranking_for_instruments(measure="volume")

        row = result.row("NEW")
        self.assertEqual(row.observations, 1)
        self.assertEqual(row.value, Decimal("10"))

    def test_a_zero_base_price_leaves_the_return_unranked(self):
        zeroed = create_instrument("ZERO")
        for trading_date, close in ((DAY1, "0"), (DAY2, "5")):
            add_price(zeroed, trading_date, close, close, close, close, 1)

        result = ranking_for_instruments(measure="return")

        row = result.row("ZERO")
        self.assertIsNone(row.value)
        self.assertIsNone(row.rank)
        self.assertEqual(row.observations, 2)
        self.assertEqual(result.unranked, 1)

    def test_instruments_are_windowed_on_their_own_history_even_with_gaps(self):
        gapped = create_instrument("GAP")
        for trading_date, close in ((DAY1, "10"), (DAY3, "11"), (DAY5, "12")):
            add_price(gapped, trading_date, close, close, close, close, 1)

        result = ranking_for_instruments(measure="return", lookback=2)

        row = result.row("GAP")
        self.assertEqual(row.observations, 2)
        self.assertEqual(row.first_date, DAY3)
        self.assertEqual(row.last_date, DAY5)
        self.assertEqual(row.value, Decimal("0.09090909"))

    def test_equal_returns_share_a_rank_across_instruments(self):
        for symbol in ("TWIN1", "TWIN2"):
            twin = create_instrument(symbol)
            for trading_date, close in ((DAY1, "100"), (DAY2, "102")):
                add_price(twin, trading_date, close, close, close, close, 1)

        result = ranking_for_instruments(measure="return")

        # GOOG 0.08, MSFT 0.05, AAPL 0.04, then the twins tied at 0.02 -- the same
        # number, so the same rank, and the next distinct value would skip to 6.
        ranks = {row.symbol: row.rank for row in result.rows}
        self.assertEqual(ranks["TWIN1"], 4)
        self.assertEqual(ranks["TWIN2"], 4)
        self.assertEqual([ranks[symbol] for symbol in EXPECTED_RETURN_ORDER], [1, 2, 3])
        self.assertEqual(result.row("TWIN2").value, Decimal("0.02"))

    def test_the_universe_accounts_for_the_limit(self):
        result = ranking_for_instruments(measure="return", limit=1)

        self.assertEqual(result.universe, 3)
        self.assertEqual(result.ranked, 3)
        self.assertEqual(result.unranked, 0)
        self.assertEqual(result.truncated, 2)
        self.assertEqual(len(result.rows), 1)

    def test_a_limit_keeps_the_absolute_ranks(self):
        result = ranking_for_instruments(measure="return", limit=2)

        self.assertEqual(
            [(row.rank, row.symbol) for row in result.rows],
            [(1, "GOOG"), (2, "MSFT")],
        )

    def test_a_limit_can_cut_an_unranked_row_off_the_end(self):
        result = ranking_for_instruments(measure="return", lookback=10, limit=1)

        self.assertEqual(result.universe, 3)
        self.assertEqual(result.unranked, 3)
        self.assertEqual(result.truncated, 2)
        self.assertEqual(len(result.rows), 1)

    def test_row_lookup_finds_one_symbol_and_names_what_is_there(self):
        result = ranking_for_instruments(measure="return")

        self.assertEqual(result.row("GOOG").rank, 1)
        with self.assertRaises(KeyError) as caught:
            result.row("TSLA")
        self.assertIn("TSLA", str(caught.exception))

    def test_a_ranking_carries_no_symbol_of_its_own(self):
        # A ranking is *about* symbols, so it cannot be a MetricResult, whose whole
        # shape is "the series of one instrument". Pinned because a refactor back to
        # the base class would quietly reintroduce a symbol that means nothing.
        result = ranking_for_instruments(measure="return")

        self.assertIsInstance(result, Ranking)
        self.assertNotIsInstance(result, MetricResult)
        self.assertFalse(hasattr(result, "symbol"))

    def test_the_ranks_are_reproducible_from_the_reported_values(self):
        result = ranking_for_instruments(measure="volume")

        for row in result.rows:
            better = [
                other
                for other in result.rows
                if other.value is not None
                and row.value is not None
                and other.value > row.value
            ]
            self.assertEqual(row.rank, 1 + len(better), row.symbol)

    def test_an_unknown_measure_is_rejected_by_name(self):
        with self.assertRaises(ValueError) as caught:
            ranking_for_instruments(measure="turnover")

        self.assertIn("turnover", str(caught.exception))
        for measure in RANKING_MEASURES:
            self.assertIn(measure, str(caught.exception))

    def test_an_unknown_backend_is_rejected(self):
        with self.assertRaises(ValueError):
            ranking_for_instruments(backend="rust")

    def test_the_row_number_backend_is_not_offered(self):
        # The measurement that declined it is on RANKING_BACKENDS; this pins the
        # decision itself so a future "sql" cannot appear without the numbers.
        with self.assertRaises(ValueError) as caught:
            ranking_for_instruments(backend="sql")

        self.assertIn("python", str(caught.exception))
        self.assertIn("pandas", str(caught.exception))

    def test_a_lookback_below_one_is_rejected(self):
        with self.assertRaises(ValueError):
            ranking_for_instruments(lookback=0)

    def test_a_negative_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            ranking_for_instruments(limit=-1)

    def test_a_reversed_range_is_rejected(self):
        with self.assertRaises(ValueError):
            ranking_for_instruments(start_date=DAY5, end_date=DAY1)
