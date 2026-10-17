"""Tests for the ETL endpoints on top of the job API: upload, report, and rules.

These exercise the upgrade the pull request did not cover — actually running a
load over HTTP and reading its data-quality verdict — without touching the job
CRUD tests in ``test_api.py``.
"""

import shutil
import tempfile
import uuid

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.ingestion.models import IngestionJob, IngestionRuleSummary
from apps.marketdata.models import PriceRecord

from .helpers import PRICE_HEADER, make_instrument

UPLOAD_URL = "/api/v1/ingestion/upload/"
JOBS_URL = "/api/v1/ingestion/"

CLEAN_ROW = [
    "AAPL",
    "2024-03-12",
    "100.00",
    "102.00",
    "99.00",
    "101.00",
    "101.00",
    1000,
    "clean",
]


def csv_bytes(rows, header=PRICE_HEADER):
    """Render rows as CSV bytes, the way a browser would upload them."""
    lines = [",".join(header)]
    lines.extend(",".join(str(value) for value in row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


class UploadEndpointTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        make_instrument("AAPL")
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)

    def upload(self, user=None, **overrides):
        body = overrides.pop("body", csv_bytes([CLEAN_ROW]))
        name = overrides.pop("name", "clean.csv")
        upload = SimpleUploadedFile(name, body, content_type="text/csv")
        data = {"file": upload, **overrides}
        with self.settings(MEDIA_ROOT=self.media):
            return self.client.post(
                UPLOAD_URL, data, format="multipart", **self.auth(user or self.user)
            )

    def test_uploading_a_clean_file_loads_it_and_returns_a_report(self):
        response = self.upload()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["job"]["status"], "succeeded")
        self.assertEqual(response.data["report"]["rows_accepted"], 1)
        self.assertEqual(PriceRecord.objects.count(), 1)

    def test_the_job_is_owned_by_the_caller_and_keeps_its_file(self):
        response = self.upload()

        job = IngestionJob.objects.get(pk=response.data["job"]["id"])
        self.assertEqual(job.created_by, self.user)
        self.assertEqual(job.files.count(), 1)

    def test_the_source_type_is_inferred_from_the_extension(self):
        response = self.upload()

        self.assertEqual(response.data["job"]["source_type"], "csv")

    def test_the_report_carries_a_verdict_for_every_rule(self):
        response = self.upload()

        rules = {row["rule"] for row in response.data["report"]["rules"]}
        self.assertEqual(len(rules), 12)

    def test_a_dirty_batch_is_partial_and_reports_the_failures(self):
        response = self.upload(
            body=csv_bytes(
                [
                    CLEAN_ROW,
                    [
                        "AAPL",
                        "2024-03-13",
                        "0.00",
                        "102.00",
                        "99.00",
                        "101.00",
                        "101.00",
                        1000,
                        "dirty",
                    ],
                ]
            )
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["job"]["status"], "partial")
        self.assertTrue(response.data["report"]["sample_failures"])

    def test_an_unsupported_extension_is_400_and_writes_no_job(self):
        response = self.upload(name="prices.parquet")

        self.assertEqual(response.status_code, 400)
        self.assertIn("file", response.data["error"]["fields"])
        self.assertEqual(IngestionJob.objects.count(), 0)

    def test_a_missing_file_is_400(self):
        with self.settings(MEDIA_ROOT=self.media):
            response = self.client.post(
                UPLOAD_URL, {}, format="multipart", **self.auth(self.user)
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("file", response.data["error"]["fields"])

    def test_uploading_requires_authentication(self):
        with self.settings(MEDIA_ROOT=self.media):
            response = self.client.post(
                UPLOAD_URL,
                {"file": SimpleUploadedFile("clean.csv", csv_bytes([CLEAN_ROW]))},
                format="multipart",
            )

        self.assertEqual(response.status_code, 401)


class ReportEndpointTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_user("intruder@example.com")
        self.job = IngestionJob.objects.create(created_by=self.user, source_type="csv")
        IngestionRuleSummary.objects.create(
            job=self.job, rule_name="negative_volume", passed=9, failed=1
        )
        IngestionRuleSummary.objects.create(
            job=self.job, rule_name="high_below_low", passed=10, failed=0
        )

    def url(self, pk=None):
        return f"{JOBS_URL}{pk or self.job.pk}/report/"

    def test_the_report_summarises_the_rules(self):
        response = self.get_as(self.user, self.url())

        self.assertEqual(response.status_code, 200)
        rule = next(
            row for row in response.data["rules"] if row["rule"] == "negative_volume"
        )
        self.assertEqual(rule["failed"], 1)
        self.assertEqual(rule["status"], "fail")

    def test_another_users_report_is_403(self):
        self.assertEqual(self.get_as(self.other, self.url()).status_code, 403)

    def test_a_nonexistent_report_is_404(self):
        self.assertEqual(self.get_as(self.user, self.url(uuid.uuid4())).status_code, 404)

    def test_the_report_requires_authentication(self):
        self.assertEqual(self.client.get(self.url()).status_code, 401)


class RuleSummaryEndpointTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_user("intruder@example.com")
        self.job = IngestionJob.objects.create(created_by=self.user, source_type="csv")
        IngestionRuleSummary.objects.create(
            job=self.job, rule_name="negative_volume", passed=9, failed=1
        )
        IngestionRuleSummary.objects.create(
            job=self.job, rule_name="high_below_low", passed=10, failed=0
        )

    def url(self, pk=None):
        return f"{JOBS_URL}{pk or self.job.pk}/rule-summaries/"

    def test_the_verdicts_are_listed_unpaginated(self):
        response = self.get_as(self.user, self.url())

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
        self.assertEqual(len(response.data), 2)

    def test_a_rule_carries_its_pass_fail_status(self):
        response = self.get_as(self.user, self.url())

        by_rule = {row["rule_name"]: row for row in response.data}
        self.assertEqual(by_rule["negative_volume"]["status"], "fail")
        self.assertEqual(by_rule["high_below_low"]["status"], "pass")

    def test_another_users_rules_are_403(self):
        self.assertEqual(self.get_as(self.other, self.url()).status_code, 403)

    def test_listing_requires_authentication(self):
        self.assertEqual(self.client.get(self.url()).status_code, 401)
