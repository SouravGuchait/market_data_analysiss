from django.contrib import admin
from .models import IngestionFile, IngestionJob, IngestionValidationError


class IngestionFileInline(admin.TabularInline):
    model = IngestionFile
    extra = 0


class ValidationErrorInline(admin.TabularInline):
    model = IngestionValidationError
    extra = 0
    fields = ("row_number", "field_name", "error_code")


@admin.register(IngestionJob)
class IngestionJobAdmin(admin.ModelAdmin):
    list_display = ("id", "source_type", "status", "created_by", "requested_at")
    list_filter = ("status", "source_type")
    search_fields = ("id", "created_by__email")
    date_hierarchy = "requested_at"
    readonly_fields = ("id", "requested_at")
    inlines = [IngestionFileInline, ValidationErrorInline]      


@admin.register(IngestionFile)
class IngestionFileAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "job", "content_type", "checksum")
    search_fields = ("original_filename", "checksum", "job__id")
    raw_id_fields = ("job",)


@admin.register(IngestionValidationError)
class IngestionValidationErrorAdmin(admin.ModelAdmin):
    list_display = ("job", "row_number", "field_name", "error_code")
    list_filter = ("error_code",)
    search_fields = ("job__id", "error_code")
    raw_id_fields = ("job",)
