"""Permissions shared across apps rather than owned by one of them."""

from rest_framework.permissions import BasePermission


class IsOwner(BasePermission):
    """Object-level check: only the row's creator may act on it.

    Applied to models that inherit from ``apps.common.models.OwnedModel``.
    """

    message = "You do not have permission to access this resource."

    def has_object_permission(self, request, view, obj):
        return obj.owner == request.user
