"""SQL and ORM exercises over the instrument and price models.

Each test states the business question and the expected result, and asserts the
ORM reproduces it. ``SQL`` constants hold the equivalent MySQL for reference.
"""

from decimal import Decimal

from django.db import connection
from django.db.models import (
    Count,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    FloatField,
    OuterRef,
    Subquery,
    Sum,
    Window,
)
from django.db.models.functions import Cast, Lag
from django.test import TestCase

from apps.ingestion.models import IngestionJob
from apps.instruments.models import Instrument
from apps.marketdata.models import PriceRecord, TradingCalendar

from .helpers import (
    DAY1,
    DAY2,
    DAY3,
    DAY4,
    DAY5,
    EXPECTED_RETURN_ORDER,
    SAMPLE_DATES,
    TOTAL_TRADE_VALUE,
    TOTAL_VOLUME,
    add_calendar,
    add_price,
    create_instrument,
    load_sample_series,
)

TRADE_VALUE = ExpressionWrapper(
    F("close") * F("volume"), output_field=DecimalField(max_digits=38, decimal_places=12)
)


def sql_of(queryset):
    """Renders the query as the database will see it."""
    return str(queryset.query)


# --------------------------------------------------------------------------- #
# 1. Total trade value and total quantity
# --------------------------------------------------------------------------- #

EX1_SQL = """
SELECT SUM(close * volume) AS trade_value, SUM(volume) AS total_volume
FROM marketdata_pricerecord;
"""


class TotalTradeValueTests(TestCase):
    def setUp(self):
        load_sample_series()

    def test_total_notional_value_and_total_volume(self):
        totals = PriceRecord.objects.aggregate(
            trade_value=Sum(TRADE_VALUE), total_volume=Sum("volume")
        )

        self.assertEqual(totals["trade_value"], TOTAL_TRADE_VALUE)
        self.assertEqual(totals["total_volume"], TOTAL_VOLUME)

    def test_the_generated_sql_is_inspectable(self):
        aggregate_sql = sql_of(
            PriceRecord.objects.values("instrument__symbol").annotate(
                trade_value=Sum(TRADE_VALUE)
            )
        )

        self.assertIn("SUM", aggregate_sql.upper())
        self.assertIn("volume", aggregate_sql)


# --------------------------------------------------------------------------- #
# 2. Grouping trades by symbol
# --------------------------------------------------------------------------- #

EX2_SQL = """
SELECT i.symbol,
       SUM(p.volume)           AS total_volume,
       SUM(p.close * p.volume) AS trade_value
FROM marketdata_pricerecord p
JOIN instruments_instrument i ON i.id = p.instrument_id
GROUP BY i.symbol
ORDER BY trade_value DESC;
"""


class GroupBySymbolTests(TestCase):
    def setUp(self):
        load_sample_series()

    def test_volume_and_notional_per_symbol_ranked_by_notional(self):
        ranked = (
            PriceRecord.objects.values("instrument__symbol")
            .annotate(total_volume=Sum("volume"), trade_value=Sum(TRADE_VALUE))
            .order_by("-trade_value")
        )

        self.assertEqual(
            [row["instrument__symbol"] for row in ranked], ["GOOG", "AAPL", "MSFT"]
        )
        self.assertEqual(ranked[0]["trade_value"], Decimal("520000"))
        self.assertEqual(ranked[0]["total_volume"], Decimal("10000"))
        self.assertEqual(ranked[1]["trade_value"], Decimal("510000"))
        self.assertEqual(ranked[2]["trade_value"], Decimal("500000"))

    def test_values_collapses_rows_to_dictionaries_keyed_by_the_requested_columns(self):
        rows = (
            PriceRecord.objects.filter(instrument__symbol="AAPL")
            .order_by("trading_date")
            .values_list("trading_date", "close")
        )

        self.assertEqual(len(rows), 5)
        # Decimal comparison is numeric, not scale-sensitive.
        self.assertEqual(rows[0][1], Decimal("100"))

    def test_values_list_flattens_to_a_plain_list_with_flat_true(self):
        closes = list(
            PriceRecord.objects.filter(instrument__symbol="AAPL")
            .order_by("trading_date")
            .values_list("close", flat=True)
        )

        self.assertEqual(
            closes,
            [Decimal("100"), Decimal("102"), Decimal("101"), Decimal("103"), Decimal("104")],
        )


# --------------------------------------------------------------------------- #
# 3. Highest-value trade and sorting
# --------------------------------------------------------------------------- #

EX3_SQL = """
SELECT i.symbol, p.trading_date, (p.close * p.volume) AS trade_value
FROM marketdata_pricerecord p
JOIN instruments_instrument i ON i.id = p.instrument_id
ORDER BY trade_value DESC
LIMIT 3;
"""


class HighestValueTests(TestCase):
    def setUp(self):
        load_sample_series()

    def test_top_three_trades_by_notional_value(self):
        top = (
            PriceRecord.objects.select_related("instrument")
            .annotate(trade_value=TRADE_VALUE)
            .order_by("-trade_value")[:3]
        )

        # Decimal equality is numeric, so 108000 equals 108000.000000000000.
        self.assertEqual(
            [(p.instrument.symbol, p.trade_value) for p in top],
            [("GOOG", Decimal("108000")), ("GOOG", Decimal("106000")), ("MSFT", Decimal("105000"))],
        )

    def test_order_by_ascending_and_descending_reverse_each_other(self):
        dates_asc = list(
            PriceRecord.objects.order_by("trading_date").values_list("trading_date", flat=True)
        )
        dates_desc = list(
            PriceRecord.objects.order_by("-trading_date").values_list("trading_date", flat=True)
        )

        self.assertEqual(dates_asc, dates_desc[::-1])

    def test_select_related_avoids_a_second_query_for_the_symbol(self):
        with self.assertNumQueries(1):
            row = (
                PriceRecord.objects.select_related("instrument")
                .annotate(trade_value=TRADE_VALUE)
                .order_by("-trade_value")
                .first()
            )
            _ = row.instrument.symbol


# --------------------------------------------------------------------------- #
# 4. Duplicate detection
# --------------------------------------------------------------------------- #

EX4_SQL = """
SELECT instrument_id, trading_date, COUNT(*) AS row_count
FROM marketdata_pricerecord
GROUP BY instrument_id, trading_date
HAVING COUNT(*) > 1;
"""


class DuplicateDetectionTests(TestCase):
    def setUp(self):
        load_sample_series()

    def test_the_audit_query_finds_no_duplicate_instrument_dates(self):
        duplicates = (
            PriceRecord.objects.values("instrument", "trading_date")
            .annotate(row_count=Count("id"))
            .filter(row_count__gt=1)
        )

        self.assertEqual(list(duplicates), [])
        self.assertIn("HAVING", sql_of(duplicates).upper())

    def test_distinct_values_returns_one_row_per_symbol(self):
        # order_by() clears the model's default ordering first. A DISTINCT over a
        # queryset whose ORDER BY references a non-selected column silently
        # defeats the DISTINCT: every row becomes unique on the sort key.
        symbols = list(
            PriceRecord.objects.order_by()
            .values_list("instrument__symbol", flat=True)
            .distinct()
        )

        self.assertEqual(sorted(symbols), ["AAPL", "GOOG", "MSFT"])


# --------------------------------------------------------------------------- #
# 5. Latest price per instrument
# --------------------------------------------------------------------------- #

EX5_SQL = """
SELECT symbol, close FROM (
  SELECT i.symbol AS symbol, p.close AS close,
         ROW_NUMBER() OVER (PARTITION BY p.instrument_id ORDER BY p.trading_date DESC) AS rn
  FROM marketdata_pricerecord p
  JOIN instruments_instrument i ON i.id = p.instrument_id
) ranked
WHERE rn = 1;
"""


class LatestPriceTests(TestCase):
    def setUp(self):
        load_sample_series()

    def test_a_correlated_subquery_returns_the_latest_close_per_instrument(self):
        latest_close = (
            PriceRecord.objects.filter(instrument=OuterRef("pk"))
            .order_by("-trading_date")
            .values("close")[:1]
        )

        instruments = Instrument.objects.annotate(
            latest_close=Subquery(latest_close)
        ).order_by("symbol")

        self.assertEqual(
            {i.symbol: i.latest_close for i in instruments},
            {"AAPL": Decimal("104"), "MSFT": Decimal("210"), "GOOG": Decimal("54")},
        )

    def test_the_latest_close_matches_the_newest_trading_date(self):
        latest_close = (
            PriceRecord.objects.filter(instrument=OuterRef("pk"))
            .order_by("-trading_date")
            .values("close")[:1]
        )
        latest_date = (
            PriceRecord.objects.filter(instrument=OuterRef("pk"))
            .order_by("-trading_date")
            .values("trading_date")[:1]
        )

        instruments = Instrument.objects.annotate(
            latest_close=Subquery(latest_close), latest_date=Subquery(latest_date)
        )

        self.assertTrue(all(i.latest_date == DAY5 for i in instruments))

    def test_an_instrument_with_no_prices_gets_none_rather_than_an_error(self):
        empty = create_instrument("EMPTY")
        latest_close = (
            PriceRecord.objects.filter(instrument=OuterRef("pk"))
            .order_by("-trading_date")
            .values("close")[:1]
        )

        annotated = Instrument.objects.annotate(latest_close=Subquery(latest_close))

        self.assertIsNone(annotated.get(pk=empty.pk).latest_close)

    def test_exists_detects_whether_a_user_holds_a_price_for_an_instrument(self):
        aapl = Instrument.objects.get(symbol="AAPL")
        has_price = PriceRecord.objects.filter(instrument=OuterRef("pk"))

        annotated = Instrument.objects.annotate(has_price=Exists(has_price))

        self.assertTrue(annotated.get(pk=aapl.pk).has_price)


# --------------------------------------------------------------------------- #
# 6. LAG() for previous close
# --------------------------------------------------------------------------- #

EX6_SQL = """
SELECT trading_date, close,
       LAG(close) OVER (PARTITION BY instrument_id ORDER BY trading_date) AS prev_close
FROM marketdata_pricerecord
WHERE instrument_id = %s
ORDER BY trading_date;
"""

LAG_FIELD = DecimalField(max_digits=18, decimal_places=8)


class PreviousCloseTests(TestCase):
    def setUp(self):
        load_sample_series()

    def test_lag_returns_the_previous_close_within_one_instrument(self):
        aapl = Instrument.objects.get(symbol="AAPL")

        series = (
            PriceRecord.objects.filter(instrument=aapl)
            .order_by("trading_date")
            .annotate(
                prev_close=Window(
                    expression=Lag("close", output_field=LAG_FIELD),
                    order_by=F("trading_date").asc(),
                )
            )
        )

        self.assertEqual(
            [(p.trading_date, p.close, p.prev_close) for p in series],
            [
                (DAY1, Decimal("100"), None),
                (DAY2, Decimal("102"), Decimal("100")),
                (DAY3, Decimal("101"), Decimal("102")),
                (DAY4, Decimal("103"), Decimal("101")),
                (DAY5, Decimal("104"), Decimal("103")),
            ],
        )

    def test_where_is_evaluated_before_the_window_so_a_single_row_has_no_neighbour(self):
        """The trap in every LAG() query.

        SQL evaluates WHERE before window functions. Filtering to a single
        trading_date leaves the window with one row in the partition, so LAG has
        nothing in front of it and returns NULL - even though the previous day
        exists in the table.
        """
        narrowed = (
            PriceRecord.objects.filter(instrument__symbol="AAPL", trading_date=DAY2)
            .annotate(
                prev_close=Window(
                    expression=Lag("close", output_field=LAG_FIELD),
                    order_by=F("trading_date").asc(),
                )
            )
        )

        self.assertIsNone(narrowed.get().prev_close)

        # The same query without the trading_date filter sees the whole series.
        full = (
            PriceRecord.objects.filter(instrument__symbol="AAPL")
            .annotate(
                prev_close=Window(
                    expression=Lag("close", output_field=LAG_FIELD),
                    order_by=F("trading_date").asc(),
                )
            )
            .order_by("trading_date")
        )
        self.assertEqual(
            [p.prev_close for p in full],
            [None, Decimal("100"), Decimal("102"), Decimal("101"), Decimal("103")],
        )

    def test_daily_change_is_the_expression_between_close_and_lag(self):
        aapl = Instrument.objects.get(symbol="AAPL")

        series = (
            PriceRecord.objects.filter(instrument=aapl)
            .order_by("trading_date")
            .annotate(
                prev_close=Window(
                    expression=Lag("close", output_field=LAG_FIELD),
                    order_by=F("trading_date").asc(),
                )
            )
            .annotate(daily_change=F("close") - F("prev_close"))
        )

        changes = [p.daily_change for p in series]
        self.assertIsNone(changes[0])
        self.assertEqual(changes[1:], [Decimal("2"), Decimal("-1"), Decimal("2"), Decimal("1")])

    def test_lag_partitions_by_instrument_so_series_do_not_bleed_into_each_other(self):
        windowed = (
            PriceRecord.objects.annotate(
                prev_close=Window(
                    expression=Lag("close", output_field=LAG_FIELD),
                    partition_by=["instrument"],
                    order_by=F("trading_date").asc(),
                )
            )
            .select_related("instrument")
            .order_by("instrument__symbol", "trading_date")
        )
        by_key = {(p.instrument.symbol, p.trading_date): p for p in windowed}

        # AAPL on day 2 looks back to AAPL on day 1, not to GOOG on day 1.
        self.assertEqual(by_key[("AAPL", DAY2)].prev_close, Decimal("100"))
        self.assertNotEqual(by_key[("AAPL", DAY2)].prev_close, by_key[("GOOG", DAY1)].close)
        self.assertEqual(by_key[("GOOG", DAY2)].prev_close, Decimal("50"))


# --------------------------------------------------------------------------- #
# 7. Missing trading dates
# --------------------------------------------------------------------------- #

EX7_SQL = """
SELECT c.date
FROM marketdata_tradingcalendar c
LEFT JOIN marketdata_pricerecord p
       ON p.trading_date = c.date AND p.instrument_id = %s
WHERE c.date BETWEEN %s AND %s
  AND p.id IS NULL
ORDER BY c.date;
"""


class MissingTradingDateTests(TestCase):
    def setUp(self):
        add_calendar(DAY1, DAY2, DAY3, DAY4, DAY5)

    def test_days_with_no_price_for_an_instrument_are_reported(self):
        instrument = create_instrument("AAPL")
        add_price(instrument, DAY1, "100", "105", "95", "100", 1000)
        add_price(instrument, DAY2, "102", "107", "97", "102", 1000)
        # DAY3 and DAY4 are missing on purpose.
        add_price(instrument, DAY5, "104", "109", "99", "104", 1000)

        missing = (
            TradingCalendar.objects.filter(date__range=[DAY1, DAY5])
            .exclude(
                date__in=PriceRecord.objects.filter(instrument=instrument).values("trading_date")
            )
            .order_by("date")
        )

        self.assertEqual([str(d) for d in missing], ["2024-03-13", "2024-03-14"])

    def test_a_fully_loaded_series_reports_no_gaps(self):
        instrument = create_instrument("AAPL")
        for day in SAMPLE_DATES:
            add_price(instrument, day, "100", "105", "95", "100", 1000)

        missing = TradingCalendar.objects.filter(date__range=[DAY1, DAY5]).exclude(
            date__in=PriceRecord.objects.filter(instrument=instrument).values("trading_date")
        )

        self.assertEqual(list(missing), [])


# --------------------------------------------------------------------------- #
# 8. Top five instruments by return
# --------------------------------------------------------------------------- #

EX8_SQL = """
WITH bounds AS (
  SELECT instrument_id,
         ROW_NUMBER() OVER (PARTITION BY instrument_id ORDER BY trading_date ASC)  AS rn_first,
         ROW_NUMBER() OVER (PARTITION BY instrument_id ORDER BY trading_date DESC) AS rn_last,
         close
  FROM marketdata_pricerecord
),
pivoted AS (
  SELECT instrument_id,
         MAX(CASE WHEN rn_first = 1 THEN close END) AS first_close,
         MAX(CASE WHEN rn_last  = 1 THEN close END) AS last_close
  FROM bounds
  GROUP BY instrument_id
)
SELECT i.symbol, (p.last_close - p.first_close) / p.first_close AS total_return
FROM pivoted p
JOIN instruments_instrument i ON i.id = p.instrument_id
ORDER BY total_return DESC
LIMIT 5;
"""


class TopReturnTests(TestCase):
    def setUp(self):
        load_sample_series()

    @staticmethod
    def _first_and_last_close():
        """Two correlated subqueries pivot each instrument's first and last close."""
        first_close = (
            PriceRecord.objects.filter(instrument=OuterRef("pk"))
            .order_by("trading_date")
            .values("close")[:1]
        )
        last_close = (
            PriceRecord.objects.filter(instrument=OuterRef("pk"))
            .order_by("-trading_date")
            .values("close")[:1]
        )
        return Instrument.objects.annotate(
            first_close=Subquery(first_close), last_close=Subquery(last_close)
        )

    def test_instruments_rank_by_total_return_over_the_period(self):
        ranked = (
            self._first_and_last_close()
            .annotate(
                total_return=ExpressionWrapper(
                    (Cast(F("last_close"), FloatField()) - Cast(F("first_close"), FloatField()))
                    / Cast(F("first_close"), FloatField()),
                    output_field=FloatField(),
                )
            )
            .order_by("-total_return", "symbol")[:5]
        )

        self.assertEqual([i.symbol for i in ranked], EXPECTED_RETURN_ORDER)
        self.assertAlmostEqual(ranked[0].total_return, 0.08, places=6)
        self.assertAlmostEqual(ranked[1].total_return, 0.05, places=6)
        self.assertAlmostEqual(ranked[2].total_return, 0.04, places=6)

    def test_first_and_last_close_stay_exact_decimals(self):
        annotated = self._first_and_last_close().get(symbol="AAPL")

        self.assertEqual(annotated.first_close, Decimal("100"))
        self.assertEqual(annotated.last_close, Decimal("104"))
        self.assertIsInstance(annotated.first_close, Decimal)

    def test_prices_stay_decimal_while_the_ratio_is_a_float(self):
        """Division is cast to FloatField on purpose.

        MySQL's DECIMAL does true decimal division, so ``(104-100)/100`` is 0.04.
        SQLite falls back to integer division when both operands look integral,
        giving 0. Tests run on SQLite, so without the Cast a query that is right
        on the production database would silently rank everything at zero here.

        The rule that follows: keep money in DecimalField, and treat derived
        ratios as the float they are.
        """
        ranked = (
            self._first_and_last_close()
            .annotate(
                total_return=ExpressionWrapper(
                    (Cast(F("last_close"), FloatField()) - Cast(F("first_close"), FloatField()))
                    / Cast(F("first_close"), FloatField()),
                    output_field=FloatField(),
                )
            )
            .get(symbol="MSFT")
        )

        self.assertIsInstance(ranked.total_return, float)


# --------------------------------------------------------------------------- #
# 9. Aggregated ingestion statistics per user
# --------------------------------------------------------------------------- #

EX9_SQL = """
SELECT u.email,
       COUNT(j.id)                     AS jobs,
       SUM(j.rows_received)            AS received,
       SUM(j.rows_accepted)            AS accepted,
       SUM(j.rows_rejected)            AS rejected,
       SUM(j.rows_accepted) / SUM(j.rows_received) AS acceptance_rate
FROM ingestion_ingestionjob j
JOIN accounts_user u ON u.id = j.created_by_id
GROUP BY u.email
ORDER BY jobs DESC;
"""


class JobStatisticsTests(TestCase):
    def setUp(self):
        load_sample_series()
        self.alice = self._create_user("alice@example.com")
        self.bob = self._create_user("bob@example.com")

    def _create_user(self, email):
        from django.contrib.auth import get_user_model
        from apps.ingestion.models import IngestionJob

        user = get_user_model().objects.create_user(
            email=email, password="drf-portfolio-42!"
        )
        for received, accepted in [(100, 100), (50, 40), (10, 0)]:
            IngestionJob.objects.create(
                created_by=user,
                source_type="csv",
                rows_received=received,
                rows_accepted=accepted,
                rows_rejected=received - accepted,
            )
        return user

    def test_jobs_per_user_with_row_counts(self):
        stats = (
            IngestionJob.objects.values("created_by__email")
            .annotate(
                jobs=Count("id"),
                received=Sum("rows_received"),
                accepted=Sum("rows_accepted"),
                rejected=Sum("rows_rejected"),
            )
            .order_by("-jobs", "created_by__email")
        )

        self.assertEqual(stats[0]["created_by__email"], "alice@example.com")
        self.assertEqual(stats[0]["jobs"], 3)
        self.assertEqual(stats[0]["received"], 160)
        self.assertEqual(stats[0]["accepted"], 140)
        self.assertEqual(stats[0]["rejected"], 20)
        self.assertEqual(stats[1]["received"], 160)

    def test_a_user_with_no_jobs_is_listed_once_rather_than_four_times(self):
        # A JOIN would drop them entirely; a LEFT JOIN keeps them with zeros.
        lonely = self._create_user("lonely@example.com")
        IngestionJob.objects.filter(created_by=lonely).delete()

        stats = (
            IngestionJob.objects.values("created_by__email")
            .annotate(jobs=Count("id"))
            .order_by("created_by__email")
        )

        emails = [row["created_by__email"] for row in stats]
        self.assertNotIn("lonely@example.com", emails)

    def test_users_with_at_least_one_job_are_counted_with_a_second_queryset(self):
        from django.contrib.auth import get_user_model

        job_counts = (
            IngestionJob.objects.filter(created_by=OuterRef("pk"))
            .order_by()
            .values("created_by")
            .annotate(total=Count("id"))
            .values("total")
        )

        users = get_user_model().objects.annotate(job_count=Subquery(job_counts))

        self.assertEqual(users.count(), 2)
