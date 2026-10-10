from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.permissions import ADMINISTRATOR_GROUP, ANALYST_GROUP

REGISTER_URL = reverse("accounts:register")
LOGIN_URL = reverse("accounts:login")
LOGOUT_URL = reverse("accounts:logout")
REFRESH_URL = reverse("accounts:token-refresh")
ME_URL = reverse("accounts:me")

CREDENTIALS = {"username": "analyst", "password": "drf-portfolio-42!"}


class AuthTestCase(TestCase):
    """Clears the throttle cache so per-view rate limits do not leak across tests."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def post_json(self, url, payload=None):
        """The API accepts JSON only, so tests must send JSON."""
        return self.client.post(url, payload, format="json")


def create_user(username, password=CREDENTIALS["password"], group=ANALYST_GROUP):
    user = User.objects.create_user(username=username, password=password)
    user.groups.add(Group.objects.get(name=group))
    return user


def login(client, credentials=CREDENTIALS):
    return client.post(LOGIN_URL, credentials, format="json").json()


class AnonymousAccessTests(AuthTestCase):
    def test_me_rejects_anonymous_users_with_401(self):
        response = self.client.get(ME_URL)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "not_authenticated")

    def test_logout_rejects_anonymous_users_with_401(self):
        response = self.post_json(LOGOUT_URL, {"refresh": "x"})

        self.assertEqual(response.status_code, 401)

    def test_unauthorized_errors_use_the_shared_envelope(self):
        response = self.client.get(ME_URL)

        self.assertEqual(list(response.json()), ["error"])
        self.assertIn("message", response.json()["error"])


class RegistrationTests(AuthTestCase):
    def test_register_creates_user_returns_tokens_and_assigns_analyst(self):
        response = self.post_json(
            REGISTER_URL,
            {
                "username": "newanalyst",
                "email": "newanalyst@example.com",
                "password": CREDENTIALS["password"],
            },
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("access", body)
        self.assertIn("refresh", body)
        self.assertEqual(body["user"]["roles"], ["analyst"])

        user = User.objects.get(username="newanalyst")
        self.assertTrue(user.check_password(CREDENTIALS["password"]))
        self.assertEqual(list(user.groups.values_list("name", flat=True)), ["analyst"])

    def test_register_never_returns_the_password(self):
        response = self.post_json(
            REGISTER_URL, {"username": "nopw", "password": CREDENTIALS["password"]}
        )

        self.assertNotIn("password", response.json()["user"])

    def test_register_rejects_a_weak_password_with_field_errors(self):
        response = self.post_json(REGISTER_URL, {"username": "weak", "password": "pw"})

        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertEqual(error["code"], "bad_request")
        self.assertIn("password", error["fields"])

    def test_register_rejects_a_duplicate_username(self):
        create_user("taken")
        response = self.post_json(
            REGISTER_URL, {"username": "taken", "password": CREDENTIALS["password"]}
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.json()["error"]["fields"])


class LoginTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        create_user("analyst")

    def test_login_with_valid_credentials_returns_tokens_and_profile(self):
        response = self.post_json(LOGIN_URL, CREDENTIALS)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("access", body)
        self.assertIn("refresh", body)
        self.assertEqual(body["user"]["username"], "analyst")
        self.assertEqual(body["user"]["roles"], ["analyst"])

    def test_login_with_wrong_password_is_401_without_leaking_which_field(self):
        response = self.post_json(
            LOGIN_URL, {"username": "analyst", "password": "wrong-password-1"}
        )

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("username", response.json()["error"]["fields"])

    def test_login_for_an_unknown_user_is_401(self):
        response = self.post_json(
            LOGIN_URL, {"username": "ghost", "password": "wrong-password-1"}
        )

        self.assertEqual(response.status_code, 401)

    def test_login_access_token_authenticates_me_endpoint(self):
        tokens = login(self.client)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = self.client.get(ME_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], "analyst")

    def test_me_rejects_a_tampered_token_with_401(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not.a.real.token")

        response = self.client.get(ME_URL)

        self.assertEqual(response.status_code, 401)

    def test_access_token_carries_role_claims(self):
        tokens = login(self.client)

        claims = AccessToken(tokens["access"]).payload
        self.assertEqual(claims["username"], "analyst")
        self.assertEqual(claims["roles"], ["analyst"])


class LogoutTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        create_user("analyst")
        tokens = login(self.client)
        self.refresh = tokens["refresh"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def test_logout_blacklists_the_refresh_token(self):
        response = self.post_json(LOGOUT_URL, {"refresh": self.refresh})
        self.assertEqual(response.status_code, 205)

        after = APIClient().post(REFRESH_URL, {"refresh": self.refresh}, format="json")
        self.assertEqual(after.status_code, 401)

    def test_logout_without_a_refresh_token_is_400(self):
        response = self.post_json(LOGOUT_URL, {})

        self.assertEqual(response.status_code, 400)
        self.assertIn("refresh", response.json()["error"]["fields"])


class RoleGroupTests(AuthTestCase):
    def test_the_migration_creates_both_role_groups(self):
        self.assertEqual(
            sorted(Group.objects.values_list("name", flat=True)),
            sorted([ANALYST_GROUP, ADMINISTRATOR_GROUP]),
        )

    def test_administrators_are_not_analysts_by_default(self):
        admin = create_user("boss", group=ADMINISTRATOR_GROUP)

        self.assertEqual(
            list(admin.groups.values_list("name", flat=True)), [ADMINISTRATOR_GROUP]
        )
