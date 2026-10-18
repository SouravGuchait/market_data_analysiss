"""Tests for the platform analytics report (administrator only).

The fixture is two owners and five jobs: two clean successes, one partial
success, one failure, and one still pending, spread over four source types and two
days. The failure carries an infrastructure error, and the two data-quality
failures carry validation findings, so both branches of the failure summary have
something to report.
"""

from datetime import datetime, timezone

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.ingestion.models import IngestionJob, IngestionValidationError
from apps.reports import services
from apps.reports.services import FAILURE_SAMPLE, TOP_USERS

PLATFORM_URL = "/api/v1/reports/platform/"
PASSWORD = "drf-portfolio-42!"


def at(day: int, hour: int, minute: int = 0, second: int = 0):
    return datetime(2024, 3, day, hour, minute, second, tzinfo=timezone.utc)


class PlatformFixtureMixin:
    def make_job(self, user, *, source_type, status, day, hour, minute=0, **fields):
        job = IngestionJob.objects.create(
            created_by=user, source_type=source_type, status=status, **fields
        )
        IngestionJob.objects.filter(pk=job.pk).update(
            requested_at=at(day, hour, minute)
        )
        return job

    def build_platform_fixture(self):
        alice = get_user_model().objects.create_user(
            email="alice@example.com", password=PASSWORD
        )
        bob = get_user_model().objects.create_user(
            email="bob@example.com", password=PASSWORD
        )

        self.make_job(
            alice, source_type="csv", status="completed", day=11, hour=10,
            started_at=at(11, 10), completed_at=at(11, 10, 0, 30),
            rows_received=100, rows_accepted=100, rows_rejected=0,
        )
        self.make_job(
            alice, source_type="csv", status="completed", day=11, hour=11,
            started_at=at(11, 11), completed_at=at(11, 11, 0, 20),
            rows_received=80, rows_accepted=80, rows_rejected=0,
        )
        failed = self.make_job(
            alice, source_type="json", status="failed", day=12, hour=8,
            started_at=at(12, 8), completed_at=at(12, 8, 0, 10),
            rows_received=10, rows_rejected=10,
            last_error="ValueError: bad header",
        )
        partial = self.make_job(
            bob, source_type="xlsx", status="completed_with_errors", day=12, hour=9,
            started_at=at(12, 9), completed_at=at(12, 9, 1, 0),
            rows_received=20, rows_accepted=15, rows_rejected=5,
        )
        self.make_job(bob, source_type="api", status="pending", day=12, hour=10)

        for row in range(2):
            IngestionValidationError.objects.create(
                job=failed, row_number=row, error_code="missing_symbol"
            )
        for row in range(4):
            IngestionValidationError.objects.create(
                job=partial, row_number=row, error_code="non_positive_price"
            )

        return alice, bob


class PlatformAnalyticsServiceTests(PlatformFixtureMixin, TestCase):
    def setUp(self):
        self.alice, self.bob = self.build_platform_fixture()
        self.report = services.get_platform_analytics()

    # --- scalar totals --------------------------------------------------- #

    def test_the_outcome_buckets_count_every_owners_jobs(self):
        totals = self.report["totals"]

        self.assertEqual(totals["jobs"], 5)
        self.assertEqual(totals["users_with_jobs"], 2)
        self.assertEqual(totals["successful"], 2)
        self.assertEqual(totals["partially_successful"], 1)
        self.assertEqual(totals["failed"], 1)
        self.assertEqual(totals["active"], 1)  # the pending job

    def test_records_are_summed_across_the_platform(self):
        records = self.report["records"]

        self.assertEqual(records["processed"], 210)  # 100 + 80 + 10 + 20 + 0
        self.assertEqual(records["accepted"], 195)  # 100 + 80 + 0 + 15 + 0
        self.assertEqual(records["rejected"], 15)  # 0 + 0 + 10 + 5 + 0

    def test_durations_average_and_maximum_across_the_platform(self):
        durations = self.report["durations"]

        self.assertEqual(durations["measured_jobs"], 4)
        self.assertEqual(durations["average_seconds"], 30.0)  # (30+20+10+60)/4
        self.assertEqual(durations["maximum_seconds"], 60.0)

    # --- grouped reads --------------------------------------------------- #

    def test_groupings_span_every_source_and_status(self):
        self.assertEqual(
            self.report["by_status"],
            {
                "completed": 2,
                "failed": 1,
                "completed_with_errors": 1,
                "pending": 1,
            },
        )
        self.assertEqual(
            self.report["by_source"], {"csv": 2, "json": 1, "xlsx": 1, "api": 1}
        )

    def test_daily_counts_cover_the_whole_platform(self):
        self.assertEqual(
            self.report["daily_job_counts"],
            [{"date": at(11, 0).date(), "count": 2}, {"date": at(12, 0).date(), "count": 3}],
        )

    # --- per-user leaderboard ------------------------------------------- #

    def test_the_leaderboard_ranks_owners_by_job_count(self):
        top = self.report["top_users"]

        self.assertEqual([row["email"] for row in top], ["alice@example.com", "bob@example.com"])
        self.assertEqual(top[0]["jobs"], 3)
        self.assertEqual(top[0]["successful"], 2)
        self.assertEqual(top[0]["failed"], 1)
        self.assertEqual(top[1]["jobs"], 2)

    def test_the_leaderboard_is_capped(self):
        # One owner per entry, so capping at N returns N rows once there are N
        # owners. Each extra owner is one more job.
        for index in range(TOP_USERS + 3):
            owner = get_user_model().objects.create_user(
                email=f"extra{index}@example.com", password=PASSWORD
            )
            self.make_job(owner, source_type="csv", status="completed", day=12, hour=12)

        self.assertEqual(len(services.get_platform_analytics()["top_users"]), TOP_USERS)

    # --- failure summary ------------------------------------------------- #

    def test_the_failure_summary_names_the_failed_jobs(self):
        failures = self.report["failures"]

        self.assertEqual(failures["failed_jobs"], 1)
        self.assertEqual(len(failures["recent"]), 1)
        recent = failures["recent"][0]
        self.assertEqual(recent["created_by_email"], "alice@example.com")
        self.assertEqual(recent["status"], "failed")
        self.assertEqual(recent["last_error"], "ValueError: bad header")

    def test_the_failure_summary_ranks_error_codes_by_volume(self):
        self.assertEqual(
            self.report["failures"]["top_error_codes"],
            [
                {"error_code": "non_positive_price", "count": 4},
                {"error_code": "missing_symbol", "count": 2},
            ],
        )

    def test_the_recent_failure_list_is_capped(self):
        for index in range(FAILURE_SAMPLE + 2):
            self.make_job(
                self.alice, source_type="csv", status="failed", day=12, hour=13,
                last_error=f"boom {index}",
            )

        recent = services.get_platform_analytics()["failures"]["recent"]
        self.assertEqual(len(recent), FAILURE_SAMPLE)

    # --- empty and cost -------------------------------------------------- #

    def test_an_empty_platform_reports_zeroes_not_an_error(self):
        IngestionJob.objects.all().delete()
        report = services.get_platform_analytics()

        self.assertEqual(report["totals"], {
            "jobs": 0,
            "users_with_jobs": 0,
            "successful": 0,
            "partially_successful": 0,
            "failed": 0,
            "active": 0,
        })
        self.assertIsNone(report["durations"]["average_seconds"])
        self.assertEqual(report["top_users"], [])
        self.assertEqual(report["failures"]["recent"], [])
        self.assertEqual(report["failures"]["top_error_codes"], [])
        self.assertEqual(report["by_status"], {})

    def test_the_report_runs_a_constant_number_of_queries(self):
        # One aggregate, three grouped reads, the leaderboard, the recent
        # failures, and the error-code ranking.
        with self.assertNumQueries(7):
            services.get_platform_analytics()


class PlatformAnalyticsAPITests(PlatformFixtureMixin, AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.alice, self.bob = self.build_platform_fixture()
        self.admin = self.create_administrator("root@example.com")

    def test_an_administrator_sees_the_platform_report(self):
        response = self.get_as(self.admin, PLATFORM_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["totals"]["jobs"], 5)
        self.assertEqual(response.data["totals"]["users_with_jobs"], 2)

    def test_the_platform_report_is_not_scoped_to_the_caller(self):
        # The admin owns no jobs, yet the report counts both other users' work:
        # the permission is the scope, deliberately.
        response = self.get_as(self.admin, PLATFORM_URL)

        emails = {row["email"] for row in response.data["top_users"]}
        self.assertEqual(emails, {"alice@example.com", "bob@example.com"})

    def test_an_analyst_is_forbidden(self):
        response = self.get_as(self.user, PLATFORM_URL)

        self.assertEqual(response.status_code, 403)

    def test_an_analyst_can_still_read_their_own_report(self):
        # The role gate is on the platform report only; /me/ stays open.
        self.assertEqual(self.get_as(self.user, "/api/v1/reports/me/").status_code, 200)

    def test_the_platform_report_requires_authentication(self):
        self.assertEqual(self.client.get(PLATFORM_URL).status_code, 401)
