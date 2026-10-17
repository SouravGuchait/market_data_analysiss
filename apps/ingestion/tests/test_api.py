"""Tests for the ingestion job endpoints."""

from django.test import TestCase

from apps.common.tests.mixins import AuthenticatedAPITestCase
from apps.ingestion.models import IngestionJob, IngestionValidationError, JobStatus
from apps.ingestion import services

JOBS_URL = "/api/v1/ingestion/"


class IngestionServiceTests(TestCase):
    """The service layer is callable without HTTP, so it is tested directly."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.owner = get_user_model().objects.create_user(
            email="owner@example.com", password="drf-portfolio-42!"
        )
        self.other = get_user_model().objects.create_user(
            email="other@example.com", password="drf-portfolio-42!"
        )

    def test_a_job_is_created_pending_with_the_caller_as_owner(self):
        job = services.create_job(created_by=self.owner, source_type="csv")

        self.assertEqual(job.status, "pending")
        self.assertEqual(job.created_by, self.owner)
        self.assertEqual(job.rows_received, 0)

    def test_an_unknown_source_type_is_rejected(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError) as ctx:
            services.create_job(created_by=self.owner, source_type="carrier-pigeon")

        self.assertIn("source_type", ctx.exception.message_dict)

    def test_a_job_lookup_by_owner_does_not_leak_across_users(self):
        job = services.create_job(created_by=self.owner, source_type="csv")

        with self.assertRaises(IngestionJob.DoesNotExist):
            services.get_job_for_user(self.other, job.pk)

        self.assertEqual(services.get_job_for_user(self.owner, job.pk), job)

    def test_a_users_job_lists_excludes_other_users_jobs(self):
        mine = services.create_job(created_by=self.owner, source_type="csv")
        services.create_job(created_by=self.other, source_type="csv")

        self.assertEqual(list(services.list_jobs_for_user(self.owner)), [mine])

    def test_the_queryset_joins_the_owner(self):
        services.create_job(created_by=self.owner, source_type="csv")

        with self.assertNumQueries(1):
            list(services.get_job_queryset())

    def test_the_user_statistics_aggregate_in_sql(self):
        job = services.create_job(created_by=self.owner, source_type="csv")
        job.rows_received, job.rows_accepted, job.rows_rejected = 10, 8, 2
        job.save()

        stats = services.get_job_statistics(self.owner)

        self.assertEqual(stats["total_jobs"], 1)
        self.assertEqual(stats["total_rows_received"], 10)
        self.assertEqual(stats["total_rows_accepted"], 8)
        self.assertEqual(stats["total_rows_rejected"], 2)


class IngestionJobCreateTests(AuthenticatedAPITestCase):
    def test_creating_a_job_returns_201_with_pending_status(self):
        response = self.post_as(self.user, JOBS_URL, {"source_type": "csv"})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "pending")
        self.assertEqual(response.data["source_type"], "csv")
        self.assertIn("id", response.data)

    def test_the_owner_is_recorded_from_the_token_not_the_body(self):
        response = self.post_as(self.user, JOBS_URL, {"source_type": "json"})

        job = IngestionJob.objects.get(pk=response.data["id"])
        self.assertEqual(job.created_by, self.user)

    def test_an_unknown_source_type_is_400(self):
        response = self.post_as(self.user, JOBS_URL, {"source_type": "carrier-pigeon"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("source_type", response.data["error"]["fields"])

    def test_a_missing_source_type_is_400(self):
        response = self.post_as(self.user, JOBS_URL, {})

        self.assertEqual(response.status_code, 400)
        self.assertIn("source_type", response.data["error"]["fields"])

    def test_a_client_cannot_set_status_or_row_counts(self):
        response = self.post_as(
            self.user,
            JOBS_URL,
            {"source_type": "csv", "status": "completed", "rows_received": 9999},
        )

        self.assertEqual(response.status_code, 201)
        job = IngestionJob.objects.get(pk=response.data["id"])
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.rows_received, 0)

    def test_creating_requires_authentication(self):
        response = self.client.post(JOBS_URL, {"source_type": "csv"}, format="json")

        self.assertEqual(response.status_code, 401)

    def test_the_response_flattens_the_owner_to_an_email(self):
        response = self.post_as(self.user, JOBS_URL, {"source_type": "csv"})

        self.assertEqual(response.data["created_by_email"], self.user.email)
        self.assertNotIn("created_by", response.data)


class IngestionJobListTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_user("someone.else@example.com")
        self.my_job = IngestionJob.objects.create(created_by=self.user, source_type="csv")
        self.other_job = IngestionJob.objects.create(created_by=self.other, source_type="json")

    def test_listing_returns_only_the_callers_own_jobs(self):
        response = self.get_as(self.user, JOBS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(self.my_job.pk))

    def test_a_second_user_sees_only_their_own_jobs(self):
        response = self.get_as(self.other, JOBS_URL)

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(self.other_job.pk))

    def test_listing_requires_authentication(self):
        self.assertEqual(self.client.get(JOBS_URL).status_code, 401)

    def test_filtering_by_status(self):
        self.my_job.status = JobStatus.PROCESSING
        self.my_job.save()

        response = self.get_as(self.user, f"{JOBS_URL}?status=processing")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["status"], "processing")

    def test_filtering_by_a_status_with_no_matches_returns_empty(self):
        response = self.get_as(self.user, f"{JOBS_URL}?status=failed")

        self.assertEqual(response.data["count"], 0)

    def test_filtering_by_source_type(self):
        response = self.get_as(self.user, f"{JOBS_URL}?source_type=csv")

        self.assertEqual(response.data["count"], 1)

    def test_ordering_is_newest_first_by_default(self):
        response = self.get_as(self.user, JOBS_URL)

        self.assertEqual(response.data["results"][0]["id"], str(self.my_job.pk))

    def test_pagination_applies(self):
        for _ in range(30):
            IngestionJob.objects.create(created_by=self.user, source_type="csv")

        response = self.get_as(self.user, f"{JOBS_URL}?page_size=10")

        self.assertEqual(len(response.data["results"]), 10)
        self.assertEqual(response.data["count"], 31)


class IngestionJobDetailTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_user("intruder@example.com")
        self.job = IngestionJob.objects.create(created_by=self.user, source_type="csv")

    def url(self, pk=None):
        return f"{JOBS_URL}{pk or self.job.pk}/"

    def test_the_owner_can_retrieve_their_job(self):
        response = self.get_as(self.user, self.url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], str(self.job.pk))
        self.assertEqual(response.data["status"], "pending")

    def test_a_nonexistent_job_is_404(self):
        import uuid

        response = self.get_as(self.user, self.url(uuid.uuid4()))

        self.assertEqual(response.status_code, 404)

    def test_another_users_job_is_403_not_404(self):
        # The caller is authenticated and the resource exists, so this is a
        # permission failure, not a missing object.
        response = self.get_as(self.other, self.url())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "permission_denied")

    def test_an_anonymous_request_is_401_not_403(self):
        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["error"]["code"], "not_authenticated")

    def test_the_detail_view_avoids_the_n1_query_for_the_owner(self):
        # Two queries: the JWT user lookup, then one JOIN for the job and its
        # owner. RetrieveAPIView does not paginate, so there is no COUNT.
        with self.assertNumQueries(2):
            response = self.get_as(self.user, self.url())
            _ = response.data["created_by_email"]


class ValidationErrorListTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_user("intruder@example.com")
        self.job = IngestionJob.objects.create(created_by=self.user, source_type="csv")
        self.other_job = IngestionJob.objects.create(created_by=self.other, source_type="csv")

        for row in (1, 2, 3):
            IngestionValidationError.objects.create(
                job=self.job, row_number=row, field_name="close",
                error_code="too_high" if row < 3 else "negative_volume",
                raw_row={"row": row},
            )
        IngestionValidationError.objects.create(
            job=self.other_job, row_number=1, field_name="open", error_code="not_a_number"
        )

    def url(self, pk=None):
        return f"{JOBS_URL}{pk or self.job.pk}/validation-errors/"

    def test_listing_returns_the_jobs_validation_errors(self):
        response = self.get_as(self.user, self.url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(response.data["results"][0]["row_number"], 1)

    def test_the_raw_row_is_returned(self):
        response = self.get_as(self.user, self.url())

        self.assertEqual(response.data["results"][0]["raw_row"], {"row": 1})

    def test_filtering_by_error_code(self):
        response = self.get_as(self.user, f"{self.url()}?error_code=negative_volume")

        self.assertEqual(response.data["count"], 1)

    def test_another_users_job_is_403(self):
        response = self.get_as(self.user, self.url(self.other_job.pk))

        self.assertEqual(response.status_code, 403)

    def test_a_nonexistent_job_is_404(self):
        import uuid

        response = self.get_as(self.user, self.url(uuid.uuid4()))

        self.assertEqual(response.status_code, 404)

    def test_anonymous_access_is_401(self):
        self.assertEqual(self.client.get(self.url()).status_code, 401)

    def test_a_job_with_no_errors_returns_an_empty_page(self):
        clean = IngestionJob.objects.create(created_by=self.user, source_type="csv")
        response = self.get_as(self.user, self.url(clean.pk))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])
