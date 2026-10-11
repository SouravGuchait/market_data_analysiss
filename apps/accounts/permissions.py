"""Role checks for the application's authorization rules.

Roles live on ``User.role`` rather than in Django ``Group``s. One column with a
choices field is a single source of truth, it shows up in the user change form
so an administrator edits it directly, and it cannot drift out of sync with a
group membership as it would with two mechanisms.

Each check starts with ``is_authenticated`` because ``AnonymousUser`` has no
``role`` attribute: without the guard an anonymous request raises AttributeError
and produces a 500 instead of a 401.
"""

from rest_framework.permissions import BasePermission

from .models import Role


class IsAnalyst(BasePermission):
    message = "Analyst role required."

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in (
            Role.ANALYST,
            Role.ADMINISTRATOR,
        )


class IsAdministrator(BasePermission):
    message = "Administrator role required."

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == Role.ADMINISTRATOR
