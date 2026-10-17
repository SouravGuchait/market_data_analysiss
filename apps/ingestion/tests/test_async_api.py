"""The asynchronous submission surface: submit, dedupe, cancel, and status.

The synchronous ``upload/`` endpoint proves the load works. This one proves the
*handoff* works: that a submission becomes a durable job row before anything is
queued, that a repeat submission is recognised as a repeat, and that a client can
see where its job got to.
"""

import shutil
import tempfile
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.ingestion import services, tasks
from apps.ingestion.models import IngestionJob, JobStatus, SourceType

from .helpers import PRICE_HEADER, make_instrument, make_user

SUBMIT_URL = "/api/v1/ingestion/submit/"
JOBS_URL = "/api/v1/ingestion/"

CLEAN_ROWS = [
    ["AAPL", "2024-03-12", "100.00", "102.00", "99.00", "101.00", "101.00", 1000, "http"],
    ["AAPL", "2024-03-13", "101.00", "103.00", "100.00", "102.00", "102.00", 1100, "http"],
]


def csv_bytes(rows, header=PRICE_HEADER):
    lines = [",".join(header)]
    lines.extend(",".join(str(value) for value in row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def upload(name="prices.csv", body=None):
    return SimpleUploadedFile(name, body or csv_bytes(CLEAN_ROWS), content_type="text/csv")


def queued_task(task_id="task-1"):
    """Patch the broker handoff so a test never needs a live Redis."""
    return mock.patch.object(
        tasks.process_ingestion_job, "delay", return_value=mock.Mock(id=task_id)
    )


# --------------------------------------------------------------------------- #
# Services: submission and idempotency
# --------------------------------------------------------------------------- #


@pytest.mark.django_db
def test_a_submission_creates_a_pending_job_and_queues_it(tmp_path):
    from django.test import override_settings

    user = make_user()
    with override_settings(MEDIA_ROOT=tmp_path), queued_task("task-abc") as delay:
        job, created = services.submit_upload(user=user, uploaded_file=upload())

    assert created is True
    assert job.status == JobStatus.PENDING
    assert job.files.count() == 1
    assert delay.call_count == 1
    assert delay.call_args.args == (str(job.pk),)
    job.refresh_from_db()
    assert job.celery_task_id == "task-abc"


@pytest.mark.django_db
def test_the_same_file_submitted_twice_is_one_job(tmp_path):
    from django.test import override_settings

    user = make_user()
    with override_settings(MEDIA_ROOT=tmp_path), queued_task() as delay:
        first, created_first = services.submit_upload(user=user, uploaded_file=upload())
        second, created_second = services.submit_upload(user=user, uploaded_file=upload())

    assert created_first is True
    assert created_second is False, "the re-upload is recognised, not re-created"
    assert first.pk == second.pk
    assert delay.call_count == 1, "and it is not queued a second time"
    assert IngestionJob.objects.count() == 1


@pytest.mark.django_db
def test_a_client_supplied_idempotency_key_governs_the_replay(tmp_path):
    """A retry after a timeout must map to the same job even if bytes differ.

    This is the whole point of letting a client name the operation: the client
    knows it is retrying the *same submission*; the server can only see bytes.
    """
    from django.test import override_settings

    user = make_user()
    with override_settings(MEDIA_ROOT=tmp_path), queued_task():
        first, created_first = services.submit_upload(
            user=user, uploaded_file=upload(), idempotency_key="op-42"
        )
        second, created_second = services.submit_upload(
            user=user,
            uploaded_file=upload(body=csv_bytes(CLEAN_ROWS[:1])),  # different bytes
            idempotency_key="op-42",
        )

    assert created_first is True
    assert created_second is False
    assert first.pk == second.pk


@pytest.mark.django_db
def test_two_users_uploading_the_same_file_get_their_own_jobs(tmp_path):
    """Idempotency is scoped to the owner, so it cannot leak a job across users."""
    from django.test import override_settings

    alice = make_user("alice@example.test")
    bob = make_user("bob@example.test")
    with override_settings(MEDIA_ROOT=tmp_path), queued_task():
        alice_job, _ = services.submit_upload(user=alice, uploaded_file=upload())
        bob_job, _ = services.submit_upload(user=bob, uploaded_file=upload())

    assert alice_job.pk != bob_job.pk
    assert alice_job.created_by == alice
    assert bob_job.created_by == bob


@pytest.mark.django_db
def test_a_broker_outage_leaves_a_durable_job_behind(tmp_path):
    """Redis down must not lose the submission — the row is already committed."""
    from django.test import override_settings

    user = make_user()
    with override_settings(MEDIA_ROOT=tmp_path):
        with mock.patch.object(
            tasks.process_ingestion_job,
            "delay",
            side_effect=ConnectionError("could not connect to redis"),
        ):
            with pytest.raises(ConnectionError):
                services.submit_upload(user=user, uploaded_file=upload())

    job = IngestionJob.objects.get()
    assert job.status == JobStatus.PENDING, "it waits in the database, not in Redis"
    assert job.files.count() == 1, "and its file is stored, ready for a requeue"


@pytest.mark.django_db
def test_a_broker_outage_is_recoverable_by_resubmitting(tmp_path):
    """The same submission can be queued later without duplicating the job."""
    from django.test import override_settings

    user = make_user()
    with override_settings(MEDIA_ROOT=tmp_path):
        with mock.patch.object(
            tasks.process_ingestion_job, "delay", side_effect=ConnectionError("down")
        ):
            with pytest.raises(ConnectionError):
                services.submit_upload(user=user, uploaded_file=upload())
        job, created = services.submit_upload(user=user, uploaded_file=upload())

    assert created is False
    assert IngestionJob.objects.count() == 1


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


class SubmitEndpointTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_user("intruder@example.com")
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        make_instrument("AAPL")

    def submit(self, user=None, **extra):
        with self.settings(MEDIA_ROOT=self.media), queued_task():
            return self.client.post(
                SUBMIT_URL,
                {"file": upload()},
                format="multipart",
                **self.auth(user or self.user),
                **extra,
            )

    def test_a_submission_returns_202_with_a_pending_job(self):
        response = self.submit()

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.data["created"])
        self.assertEqual(response.data["job"]["status"], "pending")
        self.assertEqual(response.data["job"]["source_type"], "csv")
        # Progress is reported honestly: no total is known, so no percentage.
        self.assertEqual(response.data["job"]["progress_percent"], -1)
        self.assertEqual(response.data["job"]["attempts"], 0)

    def test_resubmitting_the_same_file_returns_200_and_the_original_job(self):
        first = self.submit()
        second = self.submit()

        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.data["created"])
        self.assertEqual(first.data["job"]["id"], second.data["job"]["id"])
        self.assertEqual(IngestionJob.objects.count(), 1)

    def test_a_client_idempotency_key_makes_a_retry_safe(self):
        first = self.submit(HTTP_IDEMPOTENCY_KEY="op-7")
        second = self.submit(HTTP_IDEMPOTENCY_KEY="op-7")

        self.assertEqual(first.data["job"]["id"], second.data["job"]["id"])
        self.assertFalse(second.data["created"])

    def test_submitting_requires_authentication(self):
        response = self.client.post(
            SUBMIT_URL, {"file": upload()}, format="multipart"
        )

        self.assertEqual(response.status_code, 401)

    def test_a_job_can_be_read_back_while_it_is_queued(self):
        job_id = self.submit().data["job"]["id"]

        response = self.get_as(self.user, f"{JOBS_URL}{job_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "pending")
        self.assertIn("rows_processed", response.data)
        self.assertIn("heartbeat_at", response.data)


class CancelEndpointTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_user("intruder@example.com")
        self.job = IngestionJob.objects.create(
            created_by=self.user, source_type=SourceType.CSV
        )

    def url(self, pk=None):
        return f"{JOBS_URL}{pk or self.job.pk}/cancel/"

    def test_a_queued_job_is_cancelled(self):
        response = self.post_as(self.user, self.url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "cancelled")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.CANCELLED)

    def test_cancelling_twice_is_a_no_op(self):
        self.post_as(self.user, self.url())
        response = self.post_as(self.user, self.url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "cancelled")

    def test_another_users_job_cannot_be_cancelled(self):
        self.assertEqual(self.post_as(self.other, self.url()).status_code, 403)

    def test_cancelling_requires_authentication(self):
        self.assertEqual(self.client.post(self.url()).status_code, 401)

    def test_a_queued_job_that_was_cancelled_is_never_run(self):
        self.post_as(self.user, self.url())

        outcome = tasks.process_ingestion_job.apply(args=[str(self.job.pk)]).get()

        self.assertEqual(outcome, f"skipped:{JobStatus.CANCELLED}")
