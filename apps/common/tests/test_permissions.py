from django.test import TestCase

from apps.common.permissions import IsOwner


class IsOwnerTests(TestCase):
    def setUp(self):
        self.owner = type("User", (), {"pk": 1})()
        self.other = type("User", (), {"pk": 2})()
        self.owned_record = type("Record", (), {"owner": self.owner})()

    def request_as(self, user):
        return type("Request", (), {"user": user})()

    def test_the_owner_passes_the_object_level_check(self):
        request = self.request_as(self.owner)

        self.assertTrue(
            IsOwner().has_object_permission(request, None, self.owned_record)
        )

    def test_another_user_fails_the_object_level_check(self):
        request = self.request_as(self.other)

        self.assertFalse(
            IsOwner().has_object_permission(request, None, self.owned_record)
        )
