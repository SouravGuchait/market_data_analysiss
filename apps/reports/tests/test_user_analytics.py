"""Tests for the per-user ingestion analytics report.

The fixture is four jobs chosen so every branch of the report has a non-trivial
answer: a success, a failure, a partial success, and a job that has not started.
They are spread across two sources and two request dates, and one of them is
started at 23:59 so the daily grouping is exercised on a date boundary.
"""

from datetime import datetime, timezone

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.test import TestCase

from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.ingestion.models import (
    IngestionFile,
    IngestionJob,
    IngestionValidationError,
)
from apps.reports import services

ANALYTICS_URL = "/api/v1/reports/me/"
PASSWORD = "drf-portfolio-42!"


def at(day: int, hour: int, minute: int = 0, second: int = 0):
    return datetime(2024, 3, day, hour, minute, second, tzinfo=timezone.utc)


class UserAnalyticsFixtureMixin:
    """Builds the four-job fixture and returns the expected aggregate."""

    def make_job(self, user, *, source_type, status, day, hour, minute=0, second=0, **fields):
        job = IngestionJob.objects.create(
            created_by=user, source_type=source_type, status=status, **fields
        )
        # ``requested_at`` is ``auto_now_add``, so a chosen date has to be written
        # by an UPDATE that bypasses the auto field.
        IngestionJob.objects.filter(pk=job.pk).update(
            requested_at=at(day, hour, minute, second)
        )
        return job

    def build_fixture(self):
        user = get_user_model().objects.create_user(
            email="analyst@example.com", password=PASSWORD
        )
        completed = self.make_job(
            user, source_type="csv", status="completed", day=11, hour=10,
            started_at=at(11, 10), completed_at=at(11, 10, 0, 30),
            rows_received=100, rows_accepted=100, rows_rejected=0,
        )
        failed = self.make_job(
            user, source_type="json", status="failed", day=11, hour=23, minute=59,
            started_at=at(11, 23, 59), completed_at=at(12, 0, 0, 30),
            rows_received=50, rows_rejected=50,
        )
        partial = self.make_job(
            user, source_type="csv", status="completed_with_errors", day=12, hour=9,
            started_at=at(12, 9), completed_at=at(12, 9, 1, 30),
            rows_received=20, rows_accepted=15, rows_rejected=5,
        )
        pending = self.make_job(
            user, source_type="api", status="pending", day=12, hour=11,
        )
        return user, {"completed": completed, "failed": failed, "partial": partial, "pending": pending}


class UserAnalyticsServiceTests(UserAnalyticsFixtureMixin, TestCase):
    def setUp(self):
        self.user, self.jobs = self.build_fixture()
        self.report = services.get_user_analytics(self.user)

    # --- scalar totals --------------------------------------------------- #

    def test_the_outcome_buckets_count_the_right_statuses(self):
        totals = self.report["totals"]

        self.assertEqual(totals["jobs"], 4)
        self.assertEqual(totals["successful"], 1)
        self.assertEqual(totals["partially_successful"], 1)
        self.assertEqual(totals["failed"], 1)

    def test_records_are_summed_from_the_stored_counters(self):
        records = self.report["records"]

        self.assertEqual(records["processed"], 170)  # 100 + 50 + 20 + 0
        self.assertEqual(records["accepted"], 115)  # 100 + 0 + 15 + 0
        self.assertEqual(records["rejected"], 55)  # 0 + 50 + 5 + 0

    def test_duration_average_and_maximum_come_from_the_database(self):
        durations = self.report["durations"]

        self.assertEqual(durations["measured_jobs"], 3)  # the pending job has none
        self.assertEqual(durations["average_seconds"], 70.0)  # (30 + 90 + 90) / 3
        self.assertEqual(durations["maximum_seconds"], 90.0)

    def test_a_job_that_never_started_is_not_a_zero_second_job(self):
        # The pending job is the whole point: it is absent from measured_jobs and
        # from the average, rather than dragging the average toward zero.
        self.assertEqual(self.report["durations"]["measured_jobs"], 3)
        self.assertNotIn(None, self.report["durations"].values())

    # --- grouped reads --------------------------------------------------- #

    def test_counts_group_by_status(self):
        self.assertEqual(
            self.report["by_status"],
            {
                "completed": 1,
                "failed": 1,
                "completed_with_errors": 1,
                "pending": 1,
            },
        )

    def test_counts_group_by_source(self):
        self.assertEqual(self.report["by_source"], {"csv": 2, "json": 1, "api": 1})

    def test_daily_counts_group_by_requested_date(self):
        self.assertEqual(
            self.report["daily_job_counts"],
            [
                {"date": at(11, 0).date(), "count": 2},  # 10:00 and 23:59
                {"date": at(12, 0).date(), "count": 2},
            ],
        )

    def test_the_most_recent_job_is_the_newest_by_request_time(self):
        latest = self.report["most_recent_job"]

        self.assertEqual(latest["id"], str(self.jobs["pending"].pk))
        self.assertEqual(latest["status"], "pending")
        self.assertIsNone(latest["completed_at"])

    # --- correctness under joins ---------------------------------------- #

    def test_joining_job_children_does_not_inflate_the_counts(self):
        # Two files and three validation errors on one job. A single aggregate
        # that joins both one-to-many tables fans the job out into a 2 x 3 grid,
        # so the naive counts are all wrong -- this test makes that concrete.
        job = self.jobs["completed"]
        for index in range(2):
            IngestionFile.objects.create(
                job=job,
                original_filename=f"prices_{index}.csv",
                storage_path=f"ingestion/prices_{index}.csv",
                checksum=str(index) * 64,
                content_type="text/csv",
            )
        for row in range(3):
            IngestionValidationError.objects.create(
                job=job, row_number=row, error_code="non_positive_price"
            )

        naive = IngestionJob.objects.filter(created_by=self.user).aggregate(
            jobs=Count("id"),
            files=Count("files"),
            errors=Count("validation_errors"),
        )

        # The one job with children contributes 2 x 3 = 6 joined rows and the
        # three childless jobs contribute one row each, so the naive job count is
        # 9 where the truth is 4. The distinct file and error counts are inflated
        # the same way: 2 files count as 6, 3 errors count as 6.
        self.assertEqual(naive["jobs"], 9)
        self.assertEqual(naive["files"], 6)
        self.assertEqual(naive["errors"], 6)
        # The report never joins a child, so its job count is untouched.
        self.assertEqual(services.get_user_analytics(self.user)["totals"]["jobs"], 4)

    # --- isolation and shape -------------------------------------------- #

    def test_a_user_with_no_jobs_gets_zeroes_not_an_error(self):
        stranger = get_user_model().objects.create_user(
            email="stranger@example.com", password=PASSWORD
        )
        report = services.get_user_analytics(stranger)

        self.assertEqual(report["totals"]["jobs"], 0)
        self.assertEqual(report["records"]["processed"], 0)
        self.assertIsNone(report["durations"]["average_seconds"])
        self.assertIsNone(report["durations"]["maximum_seconds"])
        self.assertIsNone(report["most_recent_job"])
        self.assertEqual(report["by_status"], {})
        self.assertEqual(report["by_source"], {})
        self.assertEqual(report["daily_job_counts"], [])

    def test_another_users_jobs_are_never_counted(self):
        other = get_user_model().objects.create_user(
            email="other@example.com", password=PASSWORD
        )
        self.make_job(
            other, source_type="csv", status="completed", day=12, hour=12,
            started_at=at(12, 12), completed_at=at(12, 12, 5),
            rows_received=999, rows_accepted=999,
        )

        self.assertEqual(services.get_user_analytics(self.user)["totals"]["jobs"], 4)
        self.assertEqual(services.get_user_analytics(other)["totals"]["jobs"], 1)

    def test_the_report_runs_a_constant_number_of_queries(self):
        # One aggregate, one grouped read per dimension, and the latest job:
        # the count does not grow with the number of jobs in the table.
        with self.assertNumQueries(5):
            services.get_user_analytics(self.user)

    def test_the_processed_figure_uses_received_not_the_worker_progress_field(self):
        # A synchronous load writes rows_received and leaves rows_processed at 0.
        # Summing rows_processed would report "processed: 0" for real work.
        self.jobs["completed"].rows_processed = 0
        self.jobs["completed"].save(update_fields=["rows_processed"])

        self.assertEqual(services.get_user_analytics(self.user)["records"]["processed"], 170)


class UserAnalyticsAPITests(UserAnalyticsFixtureMixin, AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.reporter, self.jobs = self.build_fixture()

    def test_the_report_is_returned_for_the_caller(self):
        response = self.get_as(self.reporter, ANALYTICS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["user"]["email"], "analyst@example.com")
        self.assertEqual(response.data["totals"]["jobs"], 4)

    def test_the_reporter_only_sees_their_own_jobs(self):
        # ``self.user`` is the AuthenticatedAPITestCase's default user, who owns
        # no jobs; ``self.reporter`` owns the fixture.
        response = self.get_as(self.user, ANALYTICS_URL)

        self.assertEqual(response.data["totals"]["jobs"], 0)

    def test_an_administrator_still_only_sees_their_own_report(self):
        admin = self.create_administrator("root@example.com")
        response = self.get_as(admin, ANALYTICS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["totals"]["jobs"], 0)

    def test_dates_render_as_iso_strings(self):
        payload = self.get_as(self.reporter, ANALYTICS_URL).json()

        self.assertEqual(payload["most_recent_job"]["requested_at"], "2024-03-12T11:00:00Z")
        self.assertEqual(payload["daily_job_counts"][0]["date"], "2024-03-11")

    def test_analytics_require_authentication(self):
        self.assertEqual(self.client.get(ANALYTICS_URL).status_code, 401)
