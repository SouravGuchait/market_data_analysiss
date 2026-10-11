from types import SimpleNamespace

from django.test import TestCase

from apps.common.permissions import IsJobOwner, IsOwner


class IsOwnerUnitTests(TestCase):
    def setUp(self):
        self.owner = SimpleNamespace(pk=1, email="owner@example.com")
        self.other = SimpleNamespace(pk=2, email="other@example.com")

    def test_the_owner_passes(self):
        request = SimpleNamespace(user=self.owner)

        self.assertTrue(
            IsOwner().has_object_permission(
                request, None, SimpleNamespace(owner=self.owner)
            )
        )

    def test_another_user_fails(self):
        request = SimpleNamespace(user=self.other)

        self.assertFalse(
            IsOwner().has_object_permission(
                request, None, SimpleNamespace(owner=self.owner)
            )
        )

    def test_the_owner_field_is_overridable_for_models_that_name_it_differently(self):
        request = SimpleNamespace(user=self.owner)
        job = SimpleNamespace(created_by=self.owner)

        self.assertTrue(IsJobOwner().has_object_permission(request, None, job))

    def test_job_ownership_checks_created_by_not_owner(self):
        request = SimpleNamespace(user=self.other)
        job = SimpleNamespace(created_by=self.owner)

        self.assertFalse(IsJobOwner().has_object_permission(request, None, job))
