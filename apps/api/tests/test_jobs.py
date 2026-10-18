"""Tests for the ``/api/v1/jobs/`` ETL surface.

The queue handoff is mocked (as in the ingestion async tests) so the suite needs
no Redis; the point here is the HTTP contract, not the worker.
"""

import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.ingestion import services as ingestion_services
from apps.ingestion.models import (
    IngestionJob,
    IngestionRuleSummary,
    IngestionValidationError,
)

ETL_URL = "/api/v1/jobs/etl/"

CSV = (
    b"symbol,trading_date,open,high,low,close,adjusted_close,volume,source\n"
    b"AAPL,2024-03-12,100.00,102.00,99.00,101.00,101.00,1000,test\n"
)


def upload(name="prices.csv", body=CSV, content_type="text/csv"):
    return SimpleUploadedFile(name, body, content_type=content_type)


class JobETLTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_user("intruder@example.com")
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)

    def submit(self, user=None, file=None, **extra):
        with self.settings(MEDIA_ROOT=self.media), mock.patch.object(
            ingestion_services, "enqueue_ingestion_job"
        ):
            return self.client.post(
                ETL_URL,
                {"file": file or upload()},
                format="multipart",
                **self.auth(user or self.user),
                **extra,
            )

    def test_a_submission_returns_202_with_a_pending_job_id(self):
        response = self.submit()

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.data["created"])
        self.assertEqual(response.data["job"]["status"], "pending")
        self.assertEqual(response.data["job"]["source_type"], "csv")
        self.assertIn("id", response.data["job"])

    def test_the_owner_is_recorded_from_the_token(self):
        job_id = self.submit().data["job"]["id"]

        self.assertEqual(IngestionJob.objects.get(pk=job_id).created_by, self.user)

    def test_resubmitting_the_same_bytes_is_a_200_replay(self):
        first = self.submit()
        second = self.submit()

        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.data["created"])
        self.assertEqual(first.data["job"]["id"], second.data["job"]["id"])
        self.assertEqual(IngestionJob.objects.count(), 1)

    def test_a_missing_file_is_400(self):
        with self.settings(MEDIA_ROOT=self.media):
            response = self.client.post(
                ETL_URL, {}, format="multipart", **self.auth(self.user)
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("file", response.data["error"]["fields"])

    def test_an_unsupported_extension_is_400_and_creates_no_job(self):
        response = self.submit(file=upload(name="prices.txt"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(IngestionJob.objects.count(), 0)

    def test_anonymous_access_is_401(self):
        response = self.client.post(ETL_URL, {"file": upload()}, format="multipart")

        self.assertEqual(response.status_code, 401)


class JobDetailTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_user("intruder@example.com")
        self.job = IngestionJob.objects.create(created_by=self.user, source_type="csv")

    def url(self, job_id=None):
        return f"/api/v1/jobs/{job_id or self.job.pk}/"

    def test_the_owner_can_read_their_job(self):
        response = self.get_as(self.user, self.url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], str(self.job.pk))
        self.assertEqual(response.data["status"], "pending")

    def test_another_users_job_is_403_not_404(self):
        response = self.get_as(self.other, self.url())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "permission_denied")

    def test_a_nonexistent_job_is_404(self):
        import uuid

        self.assertEqual(self.get_as(self.user, self.url(uuid.uuid4())).status_code, 404)

    def test_anonymous_access_is_401(self):
        self.assertEqual(self.client.get(self.url()).status_code, 401)


class JobQualityTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_user("intruder@example.com")
        self.job = IngestionJob.objects.create(
            created_by=self.user, source_type="csv", status="completed_with_errors",
            rows_received=3, rows_accepted=1, rows_rejected=2,
        )
        IngestionRuleSummary.objects.create(
            job=self.job, rule_name="non_positive_price", passed=2, failed=1
        )
        IngestionValidationError.objects.create(
            job=self.job, row_number=2, field_name="open",
            error_code="non_positive_price", detail="open=0.00 must be greater than zero",
        )

    def url(self, job_id=None):
        return f"/api/v1/jobs/{job_id or self.job.pk}/quality/"

    def test_the_owner_gets_the_report(self):
        response = self.get_as(self.user, self.url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["job_id"], str(self.job.pk))
        self.assertEqual(response.data["rows_rejected"], 2)
        self.assertEqual(response.data["rules"][0]["rule"], "non_positive_price")
        self.assertEqual(response.data["rules"][0]["status"], "fail")
        self.assertEqual(len(response.data["sample_failures"]), 1)

    def test_another_users_report_is_403(self):
        self.assertEqual(self.get_as(self.other, self.url()).status_code, 403)

    def test_a_nonexistent_job_is_404(self):
        import uuid

        self.assertEqual(self.get_as(self.user, self.url(uuid.uuid4())).status_code, 404)

    def test_anonymous_access_is_401(self):
        self.assertEqual(self.client.get(self.url()).status_code, 401)
