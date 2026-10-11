"""Integration coverage for object-level ownership against a real model.

``IngestionJob.created_by`` is the actual ownership column, so this exercises the
permission against a queried row rather than a stand-in.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import path, reverse
from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken

from apps.common.permissions import IsJobOwner
from apps.ingestion.models import IngestionJob

PASSWORD = "drf-portfolio-42!"


class JobDetailView(APIView):
    permission_classes = [IsJobOwner]

    def get(self, request, job_id):
        job = IngestionJob.objects.get(pk=job_id)
        self.check_object_permissions(request, job)
        return Response({"id": str(job.id), "status": job.status})


urlpatterns = [
    path("test/jobs/<uuid:job_id>/", JobDetailView.as_view()),
]

OWNERSHIP_URLCONF = "apps.common.tests.test_ownership"


@override_settings(ROOT_URLCONF=OWNERSHIP_URLCONF)
class JobOwnershipTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user(
            email="owner@example.com", password=PASSWORD
        )
        self.other = get_user_model().objects.create_user(
            email="intruder@example.com", password=PASSWORD
        )
        self.job = IngestionJob.objects.create(
            created_by=self.owner, source_type="csv"
        )

    def bearer(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {AccessToken.for_user(user)}"}

    def test_the_owner_may_read_their_own_job(self):
        response = self.client.get(
            f"/test/jobs/{self.job.id}/", **self.bearer(self.owner)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending")

    def test_another_user_cannot_read_someone_elses_job(self):
        response = self.client.get(
            f"/test/jobs/{self.job.id}/", **self.bearer(self.other)
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")

    def test_an_administrator_is_not_exempt_from_ownership(self):
        administrator = get_user_model().objects.create_superuser(
            email="admin@example.com", password=PASSWORD
        )

        response = self.client.get(
            f"/test/jobs/{self.job.id}/", **self.bearer(administrator)
        )

        self.assertEqual(response.status_code, 403)
