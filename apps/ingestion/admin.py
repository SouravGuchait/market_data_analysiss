from django.contrib import admin

from .models import (
    IngestionFile,
    IngestionJob,
    IngestionRuleSummary,
    IngestionValidationError,
)
from .models import (
    IngestionFile,
    IngestionJob,
    IngestionRuleSummary,
    IngestionValidationError,
)


class IngestionFileInline(admin.TabularInline):
    model = IngestionFile
    extra = 0


class ValidationErrorInline(admin.TabularInline):
    model = IngestionValidationError
    extra = 0
    fields = ("row_number", "field_name", "error_code", "detail")


class RuleSummaryInline(admin.TabularInline):
    model = IngestionRuleSummary
    extra = 0
    fields = ("rule_name", "passed", "failed")
    readonly_fields = ("rule_name", "passed", "failed")
    fields = ("row_number", "field_name", "error_code", "detail")


class RuleSummaryInline(admin.TabularInline):
    model = IngestionRuleSummary
    extra = 0
    fields = ("rule_name", "passed", "failed")
    readonly_fields = ("rule_name", "passed", "failed")


@admin.register(IngestionJob)
class IngestionJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source_type",
        "status",
        "attempts",
        "rows_processed",
        "created_by",
        "requested_at",
        "heartbeat_at",
    )
    list_filter = ("status", "source_type")
    search_fields = (
        "id",
        "created_by__email",
        "idempotency_key",
        "celery_task_id",
    )
    date_hierarchy = "requested_at"
    # Everything the worker owns is read-only: the admin is for looking at a job,
    # and hand-editing `policy` or `checkpoint` would change what the next
    # attempt actually does.
    readonly_fields = (
        "id",
        "requested_at",
        "started_at",
        "completed_at",
        "status",
        "idempotency_key",
        "celery_task_id",
        "attempts",
        "rows_processed",
        "heartbeat_at",
        "last_error",
        "checkpoint",
        "policy",
    )
    inlines = [IngestionFileInline, ValidationErrorInline, RuleSummaryInline]


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


@admin.register(IngestionRuleSummary)
class IngestionRuleSummaryAdmin(admin.ModelAdmin):
    list_display = ("job", "rule_name", "passed", "failed")
    list_filter = ("rule_name",)
    search_fields = ("job__id", "rule_name")
    raw_id_fields = ("job",)


@admin.register(IngestionRuleSummary)
class IngestionRuleSummaryAdmin(admin.ModelAdmin):
    list_display = ("job", "rule_name", "passed", "failed")
    list_filter = ("rule_name",)
    search_fields = ("job__id", "rule_name")
    raw_id_fields = ("job",)
