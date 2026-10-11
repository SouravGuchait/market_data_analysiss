from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "object_type", "object_id", "request_id")
    list_filter = ("action", "object_type")
    search_fields = ("user__email", "action", "request_id", "object_id")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at",)
    raw_id_fields = ("user",)

    def has_add_permission(self, request):
        # Audit rows are written by the application, never by hand.
        return False

    def has_change_permission(self, request, obj=None):
        return False
