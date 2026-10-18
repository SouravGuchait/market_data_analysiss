"""Integration tests: the operations that must actually write audit events.

Everything in the vocabulary is exercised through its real entry point -- the
ingestion HTTP surface and the Django admin role form -- so the wiring, not just
the recorder, is under test. The last case pins the boundary with
ingestion-job status: the pipeline moves a job through its status machine many
times, and none of those moves is an audit event.
"""

import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.audit.events import AuditAction
from apps.audit.models import AuditEvent
from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.ingestion import tasks
from apps.ingestion.models import IngestionJob, JobStatus, SourceType
from apps.ingestion.tests.helpers import PRICE_HEADER, make_instrument

PASSWORD = "drf-portfolio-42!"
JOBS_URL = "/api/v1/ingestion/"
UPLOAD_URL = "/api/v1/ingestion/upload/"
SUBMIT_URL = "/api/v1/ingestion/submit/"

CLEAN_ROW = [
    "AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "clean",
]


def csv_bytes(rows, header=PRICE_HEADER):
    lines = [",".join(header)]
    lines.extend(",".join(str(value) for value in row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def upload_file(name="clean.csv", rows=None):
    return SimpleUploadedFile(
        name, csv_bytes(rows or [CLEAN_ROW]), content_type="text/csv"
    )


def queued_task(task_id="task-1"):
    """Patch the broker handoff so a submit test never needs a live Redis."""
    return mock.patch.object(
        tasks.process_ingestion_job, "delay", return_value=mock.Mock(id=task_id)
    )


class IngestionAuditTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        make_instrument("AAPL")

    def event(self, action):
        return AuditEvent.objects.get(user=self.user, action=action)

    def test_creating_a_job_records_it(self):
        response = self.post_as(self.user, JOBS_URL, {"source_type": "csv"})

        self.assertEqual(response.status_code, 201)
        event = self.event(AuditAction.JOB_CREATED)
        self.assertEqual(event.object_type, "IngestionJob")
        self.assertEqual(event.object_id, response.data["id"])
        self.assertEqual(event.metadata, {"source_type": "csv"})

    def test_an_upload_records_the_load_and_its_outcome(self):
        with self.settings(MEDIA_ROOT=self.media):
            response = self.client.post(
                UPLOAD_URL,
                {"file": upload_file()},
                format="multipart",
                **self.auth(self.user),
            )

        self.assertEqual(response.status_code, 201)
        event = self.event(AuditAction.UPLOAD_LOADED)
        self.assertEqual(event.object_id, response.data["job"]["id"])
        self.assertEqual(event.metadata["status"], "completed")
        self.assertEqual(event.metadata["rows_accepted"], 1)

    def test_submitting_a_file_records_the_queueing(self):
        with self.settings(MEDIA_ROOT=self.media), queued_task():
            response = self.client.post(
                SUBMIT_URL,
                {"file": upload_file()},
                format="multipart",
                **self.auth(self.user),
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            self.event(AuditAction.JOB_QUEUED).object_id, response.data["job"]["id"]
        )

    def test_a_replayed_submission_does_not_record_a_second_queueing(self):
        with self.settings(MEDIA_ROOT=self.media), queued_task():
            self.client.post(
                SUBMIT_URL, {"file": upload_file()}, format="multipart", **self.auth(self.user)
            )
            self.client.post(
                SUBMIT_URL, {"file": upload_file()}, format="multipart", **self.auth(self.user)
            )

        self.assertEqual(
            AuditEvent.objects.filter(action=AuditAction.JOB_QUEUED).count(), 1
        )

    def test_cancelling_a_job_records_it_once(self):
        job = IngestionJob.objects.create(created_by=self.user, source_type=SourceType.CSV)

        self.post_as(self.user, f"{JOBS_URL}{job.pk}/cancel/")
        self.post_as(self.user, f"{JOBS_URL}{job.pk}/cancel/")  # the no-op retry

        events = AuditEvent.objects.filter(action=AuditAction.JOB_CANCELLED)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().object_id, str(job.pk))

    def test_a_refused_cancellation_records_nothing(self):
        other = self.create_user("intruder@example.com")
        job = IngestionJob.objects.create(created_by=other, source_type=SourceType.CSV)

        self.post_as(self.user, f"{JOBS_URL}{job.pk}/cancel/")

        self.assertEqual(AuditEvent.objects.count(), 0)


class RoleChangeAuditTests(TestCase):
    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_superuser(
            email="root@example.com", password=PASSWORD
        )
        self.client.force_login(self.admin)
        self.analyst = get_user_model().objects.create_user(
            email="analyst@example.com", password=PASSWORD
        )

    def submit_user_form(self, role):
        return self.client.post(
            reverse("admin:accounts_user_change", args=[self.analyst.pk]),
            {
                "email": "analyst@example.com",
                "role": role,
                "is_active": "on",
                "is_staff": "on",
                "is_superuser": "",
                "date_joined_0": "2026-01-01",
                "date_joined_1": "00:00:00",
                "last_login_0": "",
                "last_login_1": "",
                "initial-date_joined_0": "2026-01-01",
                "initial-date_joined_1": "00:00:00",
            },
        )

    def test_promoting_a_user_records_who_did_it_and_the_move(self):
        self.assertEqual(self.submit_user_form("administrator").status_code, 302)

        event = AuditEvent.objects.get(action=AuditAction.ROLE_CHANGED)
        # The actor is the administrator, not the account that was edited.
        self.assertEqual(event.user, self.admin)
        self.assertEqual(event.object_type, "User")
        self.assertEqual(event.object_id, str(self.analyst.pk))
        self.assertEqual(event.metadata["from_role"], "analyst")
        self.assertEqual(event.metadata["to_role"], "administrator")
        self.assertEqual(event.metadata["email"], "analyst@example.com")

    def test_editing_a_user_without_changing_the_role_records_nothing(self):
        self.submit_user_form("administrator")
        AuditEvent.objects.all().delete()

        self.assertEqual(self.submit_user_form("administrator").status_code, 302)

        self.assertEqual(AuditEvent.objects.count(), 0)


class JobStatusIsNotAuditTests(TestCase):
    def test_a_job_transition_is_not_itself_an_audit_event(self):
        user = get_user_model().objects.create_user(
            email="analyst@example.com", password=PASSWORD
        )
        job = IngestionJob.objects.create(created_by=user, source_type=SourceType.CSV)
        job.status = JobStatus.PROCESSING
        job.save(update_fields=["status"])

        self.assertEqual(AuditEvent.objects.count(), 0)
