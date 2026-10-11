"""Reference data: the venues an instrument trades on, and the instruments."""

from django.db import models


class Exchange(models.Model):
    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=128)
    timezone = models.CharField(max_length=64)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class AssetType(models.TextChoices):
    EQUITY = "equity", "Equity"
    ETF = "etf", "ETF"
    CRYPTO = "crypto", "Cryptocurrency"
    FOREX = "forex", "Forex"
    INDEX = "index", "Index"
    FUTURE = "future", "Future"


class Instrument(models.Model):
    symbol = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255)
    asset_type = models.CharField(max_length=16, choices=AssetType.choices)

    # PROTECT, never CASCADE: deleting an exchange must not silently delete
    # every instrument listed on it, and with it their price history.
    exchange = models.ForeignKey(
        Exchange, on_delete=models.PROTECT, related_name="instruments"
    )
    currency = models.CharField(max_length=8)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["symbol"]
        indexes = [
            # Screener queries filter by asset class first, so it is the
            # leading column.
            models.Index(fields=["asset_type"], name="idx_instrument_asset_type"),
        ]

    def __str__(self):
        return self.symbol
