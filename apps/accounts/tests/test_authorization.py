"""Authorization and throttling coverage.

Role and object rules are exercised against throwaway views registered in this
module's own urlconf alongside the real auth routes, so no production endpoint
has to exist yet.
"""

from unittest import mock

from django.test import override_settings
from django.urls import include, path

from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.permissions import ADMINISTRATOR_GROUP, IsAdministrator, IsAnalyst
from apps.common.permissions import IsOwner

from .test_authentication import AuthTestCase, create_user, login


class AdministratorOnlyView(APIView):
    permission_classes = [IsAdministrator]

    def get(self, request):
        return Response({"ok": True})


class AnalystOnlyView(APIView):
    permission_classes = [IsAnalyst]

    def get(self, request):
        return Response({"ok": True})


class Record:
    """Stand-in for the ingestion and analytics models that inherit OwnedModel."""

    def __init__(self, owner):
        self.owner = owner


class OwnedRecordView(APIView):
    permission_classes = [IsOwner]

    def get(self, request, record_id):
        record = RECORDS[record_id]
        self.check_object_permissions(request, record)
        return Response({"owner": record.owner.username})


RECORDS = {}

urlpatterns = [
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("test/administrator-only/", AdministratorOnlyView.as_view()),
    path("test/analyst-only/", AnalystOnlyView.as_view()),
    path("test/records/<int:record_id>/", OwnedRecordView.as_view()),
]

AUTHORIZATION_URLCONF = "apps.accounts.tests.test_authorization"


def bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


@override_settings(ROOT_URLCONF=AUTHORIZATION_URLCONF)
class RoleRestrictionTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.analyst = create_user("analyst")
        self.administrator = create_user("boss", group=ADMINISTRATOR_GROUP)
        RECORDS[1] = Record(self.analyst)
        RECORDS[2] = Record(self.administrator)

    def test_analyst_may_reach_an_analyst_endpoint(self):
        response = self.client.get("/test/analyst-only/", **bearer(self.analyst))

        self.assertEqual(response.status_code, 200)

    def test_analyst_is_forbidden_from_an_administrator_endpoint(self):
        response = self.client.get(
            "/test/administrator-only/", **bearer(self.analyst)
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")

    def test_administrator_may_reach_an_administrator_endpoint(self):
        response = self.client.get(
            "/test/administrator-only/", **bearer(self.administrator)
        )

        self.assertEqual(response.status_code, 200)

    def test_anonymous_users_get_401_not_403(self):
        response = self.client.get("/test/administrator-only/")

        self.assertEqual(response.status_code, 401)

    def test_the_owner_may_read_their_own_record(self):
        response = self.client.get("/test/records/1/", **bearer(self.analyst))

        self.assertEqual(response.status_code, 200)

    def test_another_user_cannot_read_someone_elses_record(self):
        response = self.client.get("/test/records/2/", **bearer(self.analyst))

        self.assertEqual(response.status_code, 403)

    def test_the_administrator_cannot_read_another_users_record(self):
        response = self.client.get("/test/records/1/", **bearer(self.administrator))

        self.assertEqual(response.status_code, 403)


THROTTLED_RATES = {"login": "2/min", "register": "5/hour"}


@override_settings(ROOT_URLCONF=AUTHORIZATION_URLCONF)
class ThrottlingTests(AuthTestCase):
    """Patches the class attribute the throttles actually read.

    ``SimpleRateThrottle.THROTTLE_RATES`` is bound when the module is imported,
    so overriding ``REST_FRAMEWORK`` alone refreshes ``api_settings`` but leaves
    the throttles reading the production rates.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            SimpleRateThrottle, "THROTTLE_RATES", THROTTLED_RATES
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_login_is_throttled_after_repeated_failures(self):
        bad = {"username": "analyst", "password": "wrong-password-1"}

        for _ in range(2):
            response = self.post_json("/api/v1/auth/login/", bad)
            self.assertEqual(response.status_code, 401)

        response = self.post_json("/api/v1/auth/login/", bad)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "throttled")

    def test_register_is_throttled_after_the_hourly_limit(self):
        for index in range(5):
            response = self.post_json(
                "/api/v1/auth/register/",
                {"username": f"signup{index}", "password": "drf-portfolio-42!"},
            )
            self.assertEqual(response.status_code, 201)

        response = self.post_json(
            "/api/v1/auth/register/",
            {"username": "signup-extra", "password": "drf-portfolio-42!"},
        )

        self.assertEqual(response.status_code, 429)

    def test_a_valid_login_is_not_throttled_below_the_limit(self):
        create_user("analyst")

        response = self.post_json(
            "/api/v1/auth/login/",
            {"username": "analyst", "password": "drf-portfolio-42!"},
        )

        self.assertEqual(response.status_code, 200)
