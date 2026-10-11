"""Permissions shared across apps rather than owned by one of them."""

from rest_framework.permissions import BasePermission


class IsOwner(BasePermission):
    """Object-level check: only the row's creator may act on it.

    The owning field's name is configurable because models disagree on what to
    call it. Override ``owner_field`` on a subclass, and keep the default
    ``owner`` for models that use that spelling directly.
    """

    owner_field = "owner"
    message = "You do not have permission to access this resource."

    def has_object_permission(self, request, view, obj):
        return getattr(obj, self.owner_field) == request.user


class IsJobOwner(IsOwner):
    """Ownership for ``IngestionJob``, which calls the field ``created_by``."""

    owner_field = "created_by"
    message = "You may only act on your own ingestion jobs."
