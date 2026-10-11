from django.contrib import admin

from .models import Exchange, Instrument


@admin.register(Exchange)
class ExchangeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "timezone")
    search_fields = ("code", "name")


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ("symbol", "name", "asset_type", "exchange", "currency", "is_active")
    list_filter = ("asset_type", "exchange", "is_active")
    search_fields = ("symbol", "name")
    raw_id_fields = ("exchange",)
    list_editable = ("is_active",)
