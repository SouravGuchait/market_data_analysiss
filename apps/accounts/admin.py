from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin
from django.contrib.auth.models import Group


admin.site.unregister(Group)


@admin.register(Group)
class GroupAdmin(GroupAdmin):
    """Shows the role groups as the authorization surface they are."""

    list_display = ("name", "permissions_count")

    @admin.display(description="permissions")
    def permissions_count(self, obj):
        return obj.permissions.count()
