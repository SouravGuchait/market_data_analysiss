"""Tests for ``/api/v1/me/`` and ``/api/v1/me/analytics/``."""

from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.ingestion.models import IngestionJob

ME_URL = "/api/v1/me/"
ME_ANALYTICS_URL = "/api/v1/me/analytics/"


class MeViewTests(AuthenticatedAPITestCase):
    def test_the_caller_gets_their_own_profile(self):
        response = self.get_as(self.user, ME_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["email"], self.user.email)
        self.assertEqual(response.data["roles"], ["analyst"])
        self.assertNotIn("password", response.data)

    def test_anonymous_access_is_401(self):
        response = self.client.get(ME_URL)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["error"]["code"], "not_authenticated")


class MeAnalyticsViewTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_user("someone.else@example.com")
        job = IngestionJob.objects.create(created_by=self.user, source_type="csv")
        job.rows_received, job.rows_accepted, job.rows_rejected = 12, 10, 2
        job.save()
        IngestionJob.objects.create(created_by=self.other, source_type="json")

    def test_the_summary_counts_only_the_callers_jobs(self):
        response = self.get_as(self.user, ME_ANALYTICS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["jobs"]["total"], 1)
        self.assertEqual(response.data["jobs"]["rows_received"], 12)
        self.assertEqual(response.data["jobs"]["by_status"], {"pending": 1})
        self.assertIn("prices", response.data)

    def test_the_other_user_sees_only_their_own_summary(self):
        response = self.get_as(self.other, ME_ANALYTICS_URL)

        self.assertEqual(response.data["jobs"]["total"], 1)
        self.assertEqual(response.data["jobs"]["rows_received"], 0)

    def test_anonymous_access_is_401(self):
        self.assertEqual(self.client.get(ME_ANALYTICS_URL).status_code, 401)
