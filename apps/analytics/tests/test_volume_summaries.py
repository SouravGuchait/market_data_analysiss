"""Volume summaries: the running total, and the period's figures.

Five sessions of 100, 300, 200, 0, 400 shares. The running total, and the session's
volume relative to the mean *so far* (``volume x n / total``, which is the same as
``volume / mean`` with no rounding in the middle):

===========  ------  ----------  -------------  ---------------  ----------
trading day  volume  total so far  sessions (n)   mean so far     relative
===========  ------  ----------  -------------  ---------------  ----------
2024-03-11   100     100           1              100              1.00000000
2024-03-12   300     400           2              200              1.50000000
2024-03-13   200     600           3              200              1.00000000
2024-03-14   0       600           4              150              0.00000000
2024-03-15   400     1000          5              200              2.00000000
===========  ------  ----------  -------------  ---------------  ----------

and the period's summary:

==============================  -------  ------------------------
quantity                        value    check
==============================  -------  ------------------------
``total_volume``                1000     the last running total
``observations``                5        one per session
``average_volume``              200      ``1000 / 5``
``highest_volume``              400      2024-03-15
``lowest_volume``               0        2024-03-14
``sessions_above_average``      2        300 and 400 (``v x 5 > 1000``)
``sessions_at_average``         1        200 (``200 x 5 == 1000``)
``sessions_below_average``      2        100 and 0
``zero_volume_sessions``        1        2024-03-14
==============================  -------  ------------------------

The 2-and-3 case is the one that shows *why* the classification does not use the
rounded average:

===========  ------  ----------  -------------  ------------------------------
trading day  volume  total      mean           2 x 2 = 4 < 5   -> below
2024-03-12   3       5          ``5 / 2 = 2.5``  3 x 2 = 6 > 5   -> above
===========  ------  ----------  -------------  ------------------------------

The average is reported as the count ``3`` (``2.5`` rounds half up), but the 3-share
session is **above** average, not at it: ``3 > 2.5``. Comparing against the reported
``3`` would have called it exactly average, which is why the summary multiplies
instead of dividing.
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
    VolumeSummary,
    format_count,
    format_decimal,
    quantize_count,
    quantize_ratio,
    summarize_volume,
    volume_series,
    volume_series_pandas,
)

SAMPLE_DATES = [DAY1, DAY2, DAY3, DAY4, DAY5]
SWING_VOLUMES = ["100", "300", "200", "0", "400"]


def decimals(values):
    return [Decimal(value) for value in values]


def build_volume_instrument(volumes):
    """One instrument carrying the given volumes on consecutive sessions."""
    instrument = create_instrument("MIXED")
    for offset, volume in enumerate(volumes):
        close = "10"
        add_price(
            instrument,
            DAY1 + timedelta(days=offset),
            close,
            close,
            close,
            close,
            int(volume),
        )
    return instrument


class VolumeSeriesUnitTests(SimpleTestCase):
    def setUp(self):
        self.points = volume_series(SAMPLE_DATES, decimals(SWING_VOLUMES))

    def test_the_running_total_at_every_session(self):
        self.assertEqual(
            [format_count(point.cumulative_volume) for point in self.points],
            ["100", "400", "600", "600", "1000"],
        )

    def test_the_relative_volume_at_every_session(self):
        self.assertEqual(
            [format_decimal(point.relative_volume) for point in self.points],
            [
                "1.00000000",
                "1.50000000",
                "1.00000000",
                "0.00000000",
                "2.00000000",
            ],
        )

    def test_the_ratio_is_against_the_mean_so_far_not_the_period_mean(self):
        """A value dated ``t`` may not use a session after ``t``. Four typical
        sessions and one heavy one: against the mean *so far* the first four are
        exactly 1, where against the period's mean they would all be 0.555..., a
        number that only the last session's volume can produce."""
        points = volume_series(
            SAMPLE_DATES, decimals(["100", "100", "100", "100", "500"])
        )
        against_period_mean = quantize_ratio(Decimal("100") / (Decimal("900") / Decimal(5)))

        self.assertEqual(
            [format_decimal(point.relative_volume) for point in points[:4]],
            ["1.00000000"] * 4,
        )
        self.assertEqual(against_period_mean, Decimal("0.55555556"))
        self.assertNotEqual(points[0].relative_volume, against_period_mean)

    def test_a_zero_volume_session_is_a_zero_ratio_not_a_missing_one(self):
        """Nothing traded, and the mean so far is 150: 0 is the answer."""
        self.assertEqual(self.points[3].volume, Decimal(0))
        self.assertEqual(self.points[3].relative_volume, Decimal(0))

    def test_a_zero_running_total_has_no_ratio(self):
        """``volume / 0`` is not a ratio, so the value is None until something trades."""
        points = volume_series(SAMPLE_DATES[:3], decimals(["0", "0", "5"]))

        self.assertEqual([point.cumulative_volume for point in points], decimals(["0", "0", "5"]))
        self.assertEqual(
            [format_decimal(point.relative_volume) for point in points],
            [None, None, "3.00000000"],
        )

    def test_volumes_are_normalised_to_counts(self):
        """A driver returns DECIMAL('100.00000000') for a BIGINT column on some
        engines and a window annotation arrives uncoverted on both; the metric
        normalises once so both render as a count."""
        points = volume_series(SAMPLE_DATES[:1], [Decimal("100.00000000")])

        self.assertEqual(format_count(points[0].volume), "100")
        self.assertEqual(str(quantize_count(Decimal("100.00000000"))), "100")

    def test_a_supplied_running_total_is_used_instead_of_the_sum(self):
        points = volume_series(
            SAMPLE_DATES,
            decimals(SWING_VOLUMES),
            cumulative=decimals(["1000", "2000", "3000", "4000", "5000"]),
        )

        self.assertEqual(points[-1].cumulative_volume, Decimal("5000"))
        # 100 / 1000 = 0.1, so the supplied total really is the one used.
        self.assertEqual(format_decimal(points[0].relative_volume), "0.10000000")

    def test_supplied_totals_that_are_the_running_total_agree_with_the_sum(self):
        scanned = volume_series(SAMPLE_DATES, decimals(SWING_VOLUMES))
        supplied = volume_series(
            SAMPLE_DATES,
            decimals(SWING_VOLUMES),
            cumulative=[point.cumulative_volume for point in scanned],
        )

        self.assertEqual(scanned, supplied)

    def test_the_pandas_reference_is_exact_past_the_float_integer_limit(self):
        """``int64`` cumsum, not ``float64``: the second total here is 2**53 + 1,
        which a float64 accumulator cannot represent -- it would give back 2**53 and
        drop a share. This is the one place the three backends could have differed,
        so it is pinned."""
        exact = 2**53
        volumes = [Decimal(exact), Decimal(1)]

        reference = volume_series(SAMPLE_DATES[:2], volumes)
        pandas = volume_series_pandas(SAMPLE_DATES[:2], volumes)

        self.assertEqual(reference[1].cumulative_volume, Decimal(exact + 1))
        self.assertEqual(pandas, reference)
        # The float path this avoids, for the record.
        self.assertEqual(int(float(exact) + 1.0), exact)

    def test_mismatched_lengths_are_rejected(self):
        with self.assertRaises(ValueError):
            volume_series(SAMPLE_DATES, decimals(SWING_VOLUMES[:2]))
        with self.assertRaises(ValueError):
            volume_series(
                SAMPLE_DATES, decimals(SWING_VOLUMES), cumulative=decimals(["1"])
            )

    def test_an_empty_series_is_an_empty_answer(self):
        self.assertEqual(volume_series([], []), [])
        self.assertEqual(volume_series_pandas([], []), [])


class VolumeSummaryUnitTests(SimpleTestCase):
    def test_the_period_figures(self):
        summary = summarize_volume(volume_series(SAMPLE_DATES, decimals(SWING_VOLUMES)))

        self.assertEqual(summary.total_volume, Decimal("1000"))
        self.assertEqual(summary.average_volume, Decimal("200"))
        self.assertEqual(summary.observations, 5)
        self.assertEqual(summary.highest_volume, Decimal("400"))
        self.assertEqual(summary.highest_volume_date, DAY5)
        self.assertEqual(summary.lowest_volume, Decimal("0"))
        self.assertEqual(summary.lowest_volume_date, DAY4)
        self.assertEqual(summary.sessions_above_average, 2)
        self.assertEqual(summary.sessions_at_average, 1)
        self.assertEqual(summary.sessions_below_average, 2)
        self.assertEqual(summary.zero_volume_sessions, 1)

    def test_the_classification_uses_the_exact_mean_not_the_rounded_one(self):
        """Volumes 2 and 3: the exact mean is 2.5, reported as the count 3. The
        3-share session is above the mean, and calling it "at" 3 would be wrong."""
        summary = summarize_volume(volume_series(SAMPLE_DATES[:2], decimals(["2", "3"])))

        self.assertEqual(summary.average_volume, Decimal("3"))
        self.assertEqual(summary.sessions_above_average, 1)
        self.assertEqual(summary.sessions_at_average, 0)
        self.assertEqual(summary.sessions_below_average, 1)

    def test_a_constant_volume_series_is_at_its_average_on_every_session(self):
        summary = summarize_volume(
            volume_series(SAMPLE_DATES, decimals(["1000"] * 5))
        )

        self.assertEqual(summary.average_volume, Decimal("1000"))
        self.assertEqual(summary.sessions_above_average, 0)
        self.assertEqual(summary.sessions_at_average, 5)
        self.assertEqual(summary.sessions_below_average, 0)

    def test_the_three_classifications_always_account_for_every_session(self):
        for volumes in (
            SWING_VOLUMES,
            ["2", "3"],
            ["0", "0", "5"],
            ["0", "0"],
            ["1"],
            ["7", "7", "7", "20", "3"],
        ):
            with self.subTest(volumes=volumes):
                summary = summarize_volume(volume_series(SAMPLE_DATES[: len(volumes)], decimals(volumes)))

                self.assertEqual(
                    summary.sessions_above_average
                    + summary.sessions_at_average
                    + summary.sessions_below_average,
                    summary.observations,
                )

    def test_a_zero_volume_session_lowers_the_mean(self):
        """Dropping the row would flatter the average to 10; keeping it is 5."""
        summary = summarize_volume(volume_series(SAMPLE_DATES[:2], decimals(["0", "10"])))

        self.assertEqual(summary.observations, 2)
        self.assertEqual(summary.total_volume, Decimal("10"))
        self.assertEqual(summary.average_volume, Decimal("5"))
        self.assertEqual(summary.zero_volume_sessions, 1)

    def test_ties_report_the_first_session_that_reached_the_extreme(self):
        summary = summarize_volume(volume_series(SAMPLE_DATES[:3], decimals(["5", "5", "1"])))

        self.assertEqual(summary.highest_volume_date, DAY1)
        self.assertEqual(summary.lowest_volume_date, DAY3)

    def test_an_all_zero_series_has_a_total_of_zero_and_everyone_at_average(self):
        summary = summarize_volume(volume_series(SAMPLE_DATES[:2], decimals(["0", "0"])))

        self.assertEqual(summary.total_volume, Decimal("0"))
        self.assertEqual(summary.average_volume, Decimal("0"))
        self.assertEqual(summary.sessions_at_average, 2)
        self.assertEqual(summary.zero_volume_sessions, 2)

    def test_the_summary_of_nothing_is_all_none(self):
        self.assertEqual(summarize_volume([]), VolumeSummary())
        self.assertIsNone(summarize_volume([]).total_volume)


class VolumeBackendAgreementTests(TestCase):
    def test_the_three_backends_agree_on_the_sample_series(self):
        instruments = load_sample_series()

        for symbol in ("AAPL", "MSFT", "GOOG"):
            with self.subTest(symbol=symbol):
                results = {
                    backend: services.volume_summaries_for_instrument(
                        instruments[symbol], backend=backend
                    )
                    for backend in services.VOLUME_BACKENDS
                }

                reference = results["python"]
                for backend, result in results.items():
                    self.assertEqual(
                        result.points, reference.points, f"{backend} points differ"
                    )
                    self.assertEqual(
                        result.summary, reference.summary, f"{backend} summary differs"
                    )

    def test_the_three_backends_agree_on_a_series_with_a_zero_volume_session(self):
        mixed = build_volume_instrument(SWING_VOLUMES)

        results = [
            services.volume_summaries_for_instrument(mixed, backend=backend)
            for backend in services.VOLUME_BACKENDS
        ]

        for result in results:
            self.assertEqual(
                [format_decimal(point.relative_volume) for point in result.points],
                [
                    "1.00000000",
                    "1.50000000",
                    "1.00000000",
                    "0.00000000",
                    "2.00000000",
                ],
            )
            self.assertEqual(result.summary.total_volume, Decimal("1000"))
            self.assertEqual(result.undefined, 0)


class VolumeServiceTests(TestCase):
    def setUp(self):
        self.instruments = load_sample_series()
        self.aapl = self.instruments["AAPL"]

    def test_a_constant_volume_series_is_at_its_average_on_every_session(self):
        result = services.volume_summaries_for_instrument(self.aapl)

        self.assertEqual(result.summary.total_volume, Decimal("5000"))
        self.assertEqual(result.summary.average_volume, Decimal("1000"))
        self.assertEqual(result.summary.observations, 5)
        self.assertEqual(result.summary.sessions_at_average, 5)
        self.assertEqual(result.summary.zero_volume_sessions, 0)
        self.assertEqual(
            [format_decimal(point.relative_volume) for point in result.points],
            ["1.00000000"] * 5,
        )

    def test_the_total_is_the_ranges_own_not_the_historys(self):
        whole = services.volume_summaries_for_instrument(self.aapl)
        from_day3 = services.volume_summaries_for_instrument(self.aapl, start_date=DAY3)

        self.assertEqual(whole.summary.total_volume, Decimal("5000"))
        self.assertEqual(whole.summary.observations, 5)

        self.assertEqual(from_day3.summary.total_volume, Decimal("3000"))
        self.assertEqual(from_day3.summary.observations, 3)
        self.assertEqual(from_day3.padding, 0)

    def test_no_warm_up_row_is_read(self):
        result = services.volume_summaries_for_instrument(self.aapl, start_date=DAY3)

        self.assertEqual(result.padding, 0)
        self.assertEqual([point.trading_date for point in result.points], [DAY3, DAY4, DAY5])
        self.assertEqual(result.points[0].cumulative_volume, Decimal("1000"))

    def test_the_whole_request_is_one_query(self):
        with self.assertNumQueries(1):
            services.volume_summaries_for_instrument(self.aapl, start_date=DAY2)

    def test_the_range_bounds_the_rows(self):
        result = services.volume_summaries_for_instrument(
            self.aapl, start_date=DAY2, end_date=DAY4
        )

        self.assertEqual([point.trading_date for point in result.points], [DAY2, DAY3, DAY4])
        self.assertEqual(result.summary.total_volume, Decimal("3000"))

    def test_the_summary_total_is_the_last_running_total(self):
        result = services.volume_summaries_for_instrument(self.aapl)

        self.assertEqual(result.summary.total_volume, result.points[-1].cumulative_volume)

    def test_a_zero_volume_opening_session_is_counted_as_undefined(self):
        """The first session traded nothing, so the ratio has no denominator; the
        rest of the range is fine and the total is still exact."""
        quiet = build_volume_instrument(["0", "0", "5"])

        result = services.volume_summaries_for_instrument(quiet)

        self.assertEqual(result.undefined, 2)
        self.assertEqual(result.summary.total_volume, Decimal("5"))
        self.assertEqual(result.summary.average_volume, Decimal("2"))
        self.assertEqual(result.summary.zero_volume_sessions, 2)
        self.assertEqual([point.cumulative_volume for point in result.points], decimals(["0", "0", "5"]))

    def test_an_instrument_without_prices_returns_no_points(self):
        empty = create_instrument("EMPTY")

        result = services.volume_summaries_for_instrument(empty)

        self.assertEqual(result.points, [])
        self.assertEqual(result.summary, VolumeSummary())
        self.assertEqual(result.undefined, 0)

    def test_an_unknown_backend_is_rejected_by_name(self):
        with self.assertRaises(ValueError) as caught:
            services.volume_summaries_for_instrument(self.aapl, backend="prophet")

        self.assertIn("prophet", str(caught.exception))
        self.assertIn("sql", str(caught.exception))
