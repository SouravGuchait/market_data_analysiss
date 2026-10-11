from django.contrib import admin

from .models import AnalyticSnapshot


@admin.register(AnalyticSnapshot)
class AnalyticSnapshotAdmin(admin.ModelAdmin):
    list_display = ("instrument", "metric_type", "as_of_date", "lookback_days", "value")
    list_filter = ("metric_type",)
    search_fields = ("instrument__symbol",)
    raw_id_fields = ("instrument",)
    date_hierarchy = "as_of_date"
