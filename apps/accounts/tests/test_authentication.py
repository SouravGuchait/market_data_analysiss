"""Authentication and authorization coverage.

Covers the email-login user model: the model itself, registration, anonymous
access, valid and invalid credentials, role restrictions, object-level ownership,
and throttling.

The throwaway views at the top are mounted through this module's own urlconf, so
no production endpoint has to exist yet for a permission class to be exercised.
"""

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import authenticate, get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import include, path, reverse

from rest_framework.response import Response
from rest_framework.test import APITestCase
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import Role
from apps.accounts.permissions import IsAnalyst, IsAdministrator
from apps.common.permissions import IsOwner


REGISTER_URL = reverse("accounts:register")
LOGIN_URL = reverse("accounts:login")
LOGOUT_URL = reverse("accounts:logout")
REFRESH_URL = reverse("accounts:token-refresh")
ME_URL = reverse("accounts:me")

CREDENTIALS = {"email": "analyst@example.com", "password": "drf-portfolio-42!"}
URLPATTERNS = {"format": "json"}


class AuthTestCase(APITestCase):
    """Clears the throttle cache so rate limits do not leak across tests.

    Extends DRF's ``APITestCase`` because its client understands ``format=``.
    Django's plain test client silently folds ``**extra`` into the WSGI environ,
    so ``format="json"`` is dropped and the request goes out as multipart.
    """

    def setUp(self):
        cache.clear()
        self.client = self.client_class()

    def post_json(self, url, payload=None):
        # The API accepts JSON only, so tests must send JSON.
        return self.client.post(url, payload, **URLPATTERNS)


def create_user(email, password=CREDENTIALS["password"], role=Role.ANALYST):
    return get_user_model().objects.create_user(email=email, password=password, role=role)


def bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {AccessToken.for_user(user)}"}


# --------------------------------------------------------------------------- #
# Test-only views, mounted by this module's urlconf
# --------------------------------------------------------------------------- #


class AdministratorOnlyView(APIView):
    permission_classes = [IsAdministrator]

    def get(self, request):
        return Response({"ok": True})


class AnalystOnlyView(APIView):
    permission_classes = [IsAnalyst]

    def get(self, request):
        return Response({"ok": True})


class Record:
    """Stand-in for models that carry an ``owner`` FK."""

    def __init__(self, owner):
        self.owner = owner


class OwnedRecordView(APIView):
    permission_classes = [IsOwner]

    def get(self, request, record_id):
        record = RECORDS[record_id]
        self.check_object_permissions(request, record)
        return Response({"owner": record.owner.email})


RECORDS = {}

urlpatterns = [
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("test/administrator-only/", AdministratorOnlyView.as_view()),
    path("test/analyst-only/", AnalystOnlyView.as_view()),
    path("test/records/<int:record_id>/", OwnedRecordView.as_view()),
]

AUTHORIZATION_URLCONF = "apps.accounts.tests.test_authentication"
THROTTLED_RATES = {"login": "2/min", "register": "5/hour"}


# --------------------------------------------------------------------------- #
# The user model
# --------------------------------------------------------------------------- #


class UserModelTests(TestCase):
    def test_email_is_the_login_identifier(self):
        self.assertEqual(get_user_model().USERNAME_FIELD, "email")

    def test_the_username_field_has_been_removed(self):
        with self.assertRaises(Exception):
            get_user_model()._meta.get_field("username")

    def test_a_user_authenticates_with_email_and_password(self):
        create_user(CREDENTIALS["email"])

        # ModelBackend reads USERNAME_FIELD, so "username=" carries the email.
        user = authenticate(username=CREDENTIALS["email"], password=CREDENTIALS["password"])

        self.assertIsNotNone(user)
        self.assertEqual(user.email, CREDENTIALS["email"])

    def test_authenticate_rejects_a_wrong_password(self):
        create_user(CREDENTIALS["email"])

        self.assertIsNone(
            authenticate(username=CREDENTIALS["email"], password="wrong-password-1")
        )

    def test_two_users_cannot_share_an_email_address(self):
        create_user("dupe@example.com")

        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            create_user("dupe@example.com")

    def test_a_superuser_is_an_administrator_by_default(self):
        admin = get_user_model().objects.create_superuser(
            email="admin@example.com", password="drf-portfolio-42!"
        )

        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_staff)
        self.assertEqual(admin.role, Role.ADMINISTRATOR)

    def test_the_default_role_is_analyst(self):
        self.assertEqual(create_user("default@example.com").role, Role.ANALYST)

    def test_the_password_is_hashed_never_stored_verbatim(self):
        user = create_user("hashed@example.com")
        user.refresh_from_db()

        self.assertNotEqual(user.password, CREDENTIALS["password"])
        self.assertTrue(user.check_password(CREDENTIALS["password"]))

    def test_str_shows_the_email(self):
        self.assertEqual(str(create_user("str@example.com")), "str@example.com")


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


class RegistrationTests(AuthTestCase):
    def test_register_creates_a_user_and_returns_tokens(self):
        response = self.post_json(
            REGISTER_URL,
            {"email": "new@example.com", "password": CREDENTIALS["password"]},
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("access", body)
        self.assertIn("refresh", body)
        self.assertEqual(body["user"]["email"], "new@example.com")
        self.assertEqual(body["user"]["roles"], [Role.ANALYST])

    def test_register_never_returns_the_password(self):
        response = self.post_json(
            REGISTER_URL, {"email": "nopw@example.com", "password": CREDENTIALS["password"]}
        )

        self.assertNotIn("password", response.json()["user"])

    def test_register_rejects_a_weak_password_with_field_errors(self):
        response = self.post_json(
            REGISTER_URL, {"email": "weak@example.com", "password": "pw"}
        )

        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertEqual(error["code"], "bad_request")
        self.assertIn("password", error["fields"])

    def test_register_rejects_an_attempt_to_self_assign_a_role(self):
        # Sending a role used to be silently ignored, which read as "accepted"
        # and hid the fact that it had no effect.
        response = self.post_json(
            REGISTER_URL,
            {
                "email": "sneaky@example.com",
                "password": CREDENTIALS["password"],
                "role": "administrator",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("role", response.json()["error"]["fields"])
        self.assertFalse(
            get_user_model().objects.filter(email="sneaky@example.com").exists()
        )

    def test_register_rejects_the_plural_roles_key_too(self):
        response = self.post_json(
            REGISTER_URL,
            {
                "email": "sneaky2@example.com",
                "password": CREDENTIALS["password"],
                "roles": "admin",
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_register_rejects_a_duplicate_email(self):
        create_user("taken@example.com")
        response = self.post_json(
            REGISTER_URL,
            {"email": "taken@example.com", "password": CREDENTIALS["password"]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json()["error"]["fields"])

    def test_every_registered_user_starts_as_an_analyst(self):
        for index in range(3):
            self.post_json(
                REGISTER_URL,
                {"email": f"bulk{index}@example.com", "password": CREDENTIALS["password"]},
            )

        self.assertEqual(
            set(get_user_model().objects.values_list("role", flat=True)),
            {Role.ANALYST},
        )


# --------------------------------------------------------------------------- #
# Anonymous access
# --------------------------------------------------------------------------- #


class AnonymousAccessTests(AuthTestCase):
    def test_me_rejects_anonymous_users_with_401(self):
        response = self.client.get(ME_URL)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "not_authenticated")

    def test_logout_rejects_anonymous_users_with_401(self):
        response = self.post_json(LOGOUT_URL, {"refresh": "x"})

        self.assertEqual(response.status_code, 401)

    def test_register_and_login_are_open_to_anonymous_users(self):
        self.assertEqual(
            self.post_json(
                REGISTER_URL, {"email": "open@example.com", "password": CREDENTIALS["password"]}
            ).status_code,
            201,
        )
        self.assertEqual(self.post_json(LOGIN_URL, CREDENTIALS).status_code, 401)


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #


class LoginTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        create_user(CREDENTIALS["email"])

    def test_login_with_valid_credentials_returns_tokens_and_profile(self):
        response = self.post_json(LOGIN_URL, CREDENTIALS)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("access", body)
        self.assertIn("refresh", body)
        self.assertEqual(body["user"]["email"], CREDENTIALS["email"])

    def test_login_requires_email_not_username(self):
        response = self.post_json(
            LOGIN_URL, {"username": CREDENTIALS["email"], "password": CREDENTIALS["password"]}
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json()["error"]["fields"])

    def test_login_with_wrong_password_is_401_without_leaking_which_field(self):
        response = self.post_json(
            LOGIN_URL, {"email": CREDENTIALS["email"], "password": "wrong-password-1"}
        )

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("email", response.json()["error"]["fields"])

    def test_login_for_an_unknown_user_is_401(self):
        response = self.post_json(
            LOGIN_URL, {"email": "ghost@example.com", "password": "wrong-password-1"}
        )

        self.assertEqual(response.status_code, 401)

    def test_access_token_authenticates_me_endpoint(self):
        tokens = self.post_json(LOGIN_URL, CREDENTIALS).json()

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = self.client.get(ME_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], CREDENTIALS["email"])

    def test_me_rejects_a_tampered_token_with_401(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not.a.real.token")

        self.assertEqual(self.client.get(ME_URL).status_code, 401)

    def test_access_token_carries_email_and_role_claims(self):
        tokens = self.post_json(LOGIN_URL, CREDENTIALS).json()
        claims = AccessToken(tokens["access"]).payload

        self.assertEqual(claims["email"], CREDENTIALS["email"])
        self.assertEqual(claims["role"], Role.ANALYST)


# --------------------------------------------------------------------------- #
# Logout
# --------------------------------------------------------------------------- #


class LogoutTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        create_user(CREDENTIALS["email"])
        tokens = self.post_json(LOGIN_URL, CREDENTIALS).json()
        self.refresh = tokens["refresh"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def test_logout_blacklists_the_refresh_token(self):
        response = self.post_json(LOGOUT_URL, {"refresh": self.refresh})
        self.assertEqual(response.status_code, 205)

        after = self.client_class().post(REFRESH_URL, {"refresh": self.refresh}, **URLPATTERNS)
        self.assertEqual(after.status_code, 401)

    def test_logout_without_a_refresh_token_is_400(self):
        response = self.post_json(LOGOUT_URL, {})

        self.assertEqual(response.status_code, 400)
        self.assertIn("refresh", response.json()["error"]["fields"])


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #


@override_settings(ROOT_URLCONF=AUTHORIZATION_URLCONF)
class RoleRestrictionTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.analyst = create_user("analyst2@example.com")
        self.administrator = create_user("admin@example.com", role=Role.ADMINISTRATOR)

    def test_an_analyst_may_reach_an_analyst_endpoint(self):
        response = self.client.get("/test/analyst-only/", **bearer(self.analyst))

        self.assertEqual(response.status_code, 200)

    def test_an_analyst_is_forbidden_from_an_administrator_endpoint(self):
        response = self.client.get(
            "/test/administrator-only/", **bearer(self.analyst)
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")

    def test_an_administrator_may_reach_an_administrator_endpoint(self):
        response = self.client.get(
            "/test/administrator-only/", **bearer(self.administrator)
        )

        self.assertEqual(response.status_code, 200)

    def test_an_administrator_satisfies_the_analyst_check(self):
        response = self.client.get("/test/analyst-only/", **bearer(self.administrator))

        self.assertEqual(response.status_code, 200)

    def test_anonymous_users_get_401_not_403(self):
        response = self.client.get("/test/administrator-only/")

        self.assertEqual(response.status_code, 401)


@override_settings(ROOT_URLCONF=AUTHORIZATION_URLCONF)
class ObjectOwnershipTests(AuthTestCase):
    """Object-level checks, the pattern ``IsOwner`` exists for."""

    def setUp(self):
        super().setUp()
        self.analyst = create_user("analyst3@example.com")
        self.other = create_user("other@example.com")
        RECORDS[1] = Record(self.analyst)
        RECORDS[2] = Record(self.other)

    def test_the_owner_may_read_their_own_record(self):
        response = self.client.get("/test/records/1/", **bearer(self.analyst))

        self.assertEqual(response.status_code, 200)

    def test_another_user_cannot_read_someone_elses_record(self):
        response = self.client.get("/test/records/2/", **bearer(self.analyst))

        self.assertEqual(response.status_code, 403)

    def test_an_administrator_is_not_exempt_from_ownership(self):
        administrator = create_user("admin2@example.com", role=Role.ADMINISTRATOR)

        response = self.client.get("/test/records/1/", **bearer(administrator))

        self.assertEqual(response.status_code, 403)

    def test_permission_denied_reports_403_not_401(self):
        self.client.force_authenticate(user=self.other)

        response = self.client.get("/test/records/1/")

        self.assertEqual(response.status_code, 403)


class IsOwnerUnitTests(TestCase):
    """``IsOwner`` checks ``obj.owner`` without touching the database."""

    def test_the_owner_passes(self):
        owner = SimpleNamespace(pk=1, email="owner@example.com")
        request = SimpleNamespace(user=owner)

        self.assertTrue(
            IsOwner().has_object_permission(request, None, SimpleNamespace(owner=owner))
        )

    def test_another_user_fails(self):
        owner = SimpleNamespace(pk=1, email="owner@example.com")
        intruder = SimpleNamespace(pk=2, email="intruder@example.com")

        self.assertFalse(
            IsOwner().has_object_permission(
                SimpleNamespace(user=intruder), None, SimpleNamespace(owner=owner)
            )
        )


# --------------------------------------------------------------------------- #
# Throttling
# --------------------------------------------------------------------------- #


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
        bad = {"email": "nobody@example.com", "password": "wrong-password-1"}

        for _ in range(2):
            self.assertEqual(self.post_json("/api/v1/auth/login/", bad).status_code, 401)

        response = self.post_json("/api/v1/auth/login/", bad)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "throttled")

    def test_register_is_throttled_after_the_hourly_limit(self):
        for index in range(5):
            response = self.post_json(
                "/api/v1/auth/register/",
                {"email": f"signup{index}@example.com", "password": CREDENTIALS["password"]},
            )
            self.assertEqual(response.status_code, 201)

        response = self.post_json(
            "/api/v1/auth/register/",
            {"email": "extra@example.com", "password": CREDENTIALS["password"]},
        )

        self.assertEqual(response.status_code, 429)

    def test_a_valid_login_is_not_throttled_below_the_limit(self):
        create_user(CREDENTIALS["email"])

        response = self.post_json("/api/v1/auth/login/", CREDENTIALS)

        self.assertEqual(response.status_code, 200)
