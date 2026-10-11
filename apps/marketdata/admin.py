from django.contrib import admin

from .models import PriceRecord, TradingCalendar


@admin.register(PriceRecord)
class PriceRecordAdmin(admin.ModelAdmin):
    list_display = (
        "instrument",
        "trading_date",
        "close",
        "volume",
        "source",
        "ingestion_job",
    )
    list_filter = ("source",)
    search_fields = ("instrument__symbol",)
    raw_id_fields = ("instrument", "ingestion_job")
    date_hierarchy = "trading_date"


@admin.register(TradingCalendar)
class TradingCalendarAdmin(admin.ModelAdmin):
    list_display = ("date", "exchange")
    list_filter = ("exchange",)
    date_hierarchy = "date"
