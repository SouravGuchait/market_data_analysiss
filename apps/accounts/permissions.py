"""Role groups and the authorization rules built on them."""

from rest_framework.permissions import BasePermission

ANALYST_GROUP = "analyst"
ADMINISTRATOR_GROUP = "administrator"


def _in_group(user, group):
    return user.groups.filter(name=group).exists()


class IsAnalyst(BasePermission):
    message = "Analyst role required."

    def has_permission(self, request, view):
        return _in_group(request.user, ANALYST_GROUP)


class IsAdministrator(BasePermission):
    message = "Administrator role required."

    def has_permission(self, request, view):
        return _in_group(request.user, ADMINISTRATOR_GROUP)
