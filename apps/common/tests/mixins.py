"""Shared pieces for API tests across apps."""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from apps.accounts.models import Role

PASSWORD = "drf-portfolio-42!"


class AuthenticatedAPITestCase(APITestCase):
    """A test case with an authenticated user and helpers to act as any user.

    Extends DRF's ``APITestCase`` because its client understands ``format=``;
    Django's plain test client silently drops it into the WSGI environ, which
    makes requests go out as multipart and earn a 415.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.user = self.create_user("user@example.com")

    def create_user(self, email, password=PASSWORD, role=Role.ANALYST, **extra):
        return get_user_model().objects.create_user(
            email=email, password=password, role=role, **extra
        )

    def create_administrator(self, email="admin@example.com"):
        return self.create_user(email, role=Role.ADMINISTRATOR)

    def auth(self, user):
        """Returns headers authenticating as ``user``."""
        return {"HTTP_AUTHORIZATION": f"Bearer {AccessToken.for_user(user)}"}

    def get_as(self, user, url, **kwargs):
        return self.client.get(url, **self.auth(user), **kwargs)

    def post_as(self, user, url, data=None, **kwargs):
        return self.client.post(url, data, format="json", **self.auth(user), **kwargs)
