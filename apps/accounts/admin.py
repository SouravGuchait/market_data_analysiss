"""Admin for the custom user model.

Django's ``UserAdmin`` hardcodes a username field, so it has to be rebuilt for a
model whose login identifier is the email address.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from apps.audit import services as audit
from apps.audit.events import AuditAction

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Roles are edited here, in the same form as every other user attribute."""

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name")}),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
        (_("Role"), {"fields": ("role",)}),
        (
            _("Permissions"),
            {
                "fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
            },
        ),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "role"),
            },
        ),
    )
    list_display = ("email", "role", "is_staff", "is_superuser", "is_active")
    list_filter = ("role", "is_staff", "is_superuser", "is_active")
    search_fields = ("email",)
    ordering = ("email",)
    filter_horizontal = ("groups", "user_permissions")

    def save_model(self, request, obj, form, change):
        """Persist the user, then audit a role change if there was one.

        A role change is the one administrative action in this app that grants or
        removes privilege, so it is the one recorded here. Only a *change* is
        logged -- creating an account is not a promotion -- and only when the role
        actually moved, which ``form.changed_data`` reports. The old value comes
        from the form's initial data, which still holds the value loaded from the
        database at that point.
        """
        previous_role = form.initial.get("role") if change else None
        super().save_model(request, obj, form, change)

        if change and "role" in form.changed_data:
            audit.record_event(
                action=AuditAction.ROLE_CHANGED,
                request=request,
                object_type="User",
                object_id=obj.pk,
                metadata={
                    "email": obj.email,
                    "from_role": previous_role,
                    "to_role": obj.role,
                },
            )
