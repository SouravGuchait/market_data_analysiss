"""Price records and the trading calendar that defines valid trading days."""

import uuid

from django.db import models

from apps.instruments.models import Instrument

PRICE_DECIMAL = {"max_digits": 18, "decimal_places": 8}


class TradingCalendar(models.Model):
    """Marks the dates an exchange was actually open.

    Distinguishes "no trades happened" from "no data was ingested", which is
    what the missing-foreign-dates exercise needs.
    """

    date = models.DateField(unique=True)
    exchange = models.ForeignKey(
        "instruments.Exchange",
        on_delete=models.CASCADE,
        related_name="trading_dates",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return str(self.date)


class PriceRecord(models.Model):
    """One OHLC observation for one instrument on one trading day.

    The fact table of the schema: append-only, and the only table here that ever
    gets large.
    """

    # A price outlives the job that imported it, so the reference is nullable
    # and clears to null rather than deleting history.
    ingestion_job = models.ForeignKey(
        "ingestion.IngestionJob",
        on_delete=models.SET_NULL,
        related_name="prices",
        null=True,
        blank=True,
    )
    instrument = models.ForeignKey(
        Instrument, on_delete=models.PROTECT, related_name="prices"
    )
    trading_date = models.DateField()

    open = models.DecimalField(**PRICE_DECIMAL)
    high = models.DecimalField(**PRICE_DECIMAL)
    low = models.DecimalField(**PRICE_DECIMAL)
    close = models.DecimalField(**PRICE_DECIMAL)

    # Null means "no adjustment available", which is genuinely unknown. A zero
    # would be a real value and would corrupt return calculations.
    adjusted_close = models.DecimalField(null=True, blank=True, **PRICE_DECIMAL)

    # BigInteger, not Decimal: equities and ETFs trade whole shares. If
    # fractional-volume crypto is added later this becomes a migration.
    volume = models.BigIntegerField()
    source = models.CharField(max_length=128)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["trading_date"]
        constraints = [
            # The duplicate-price guard. A re-ingested file must not double a row.
            models.UniqueConstraint(
                fields=["instrument", "trading_date"],
                name="uniq_price_instrument_trading_date",
            ),
            # OHLC sanity: the day's high cannot be below its low.
            models.CheckConstraint(
                condition=models.Q(high__gte=models.F("low")),
                name="chk_price_high_gte_low",
            ),
            # Enforced here rather than by a validator: validators only run in
            # full_clean() and are skipped by bulk_create and .update().
            models.CheckConstraint(
                condition=models.Q(volume__gte=0),
                name="chk_price_volume_non_negative",
            ),
        ]
        indexes = [
            # Cross-sectional queries ("everything that traded on this date")
            # cannot use the (instrument, trading_date) composite above, because
            # its leading column is unconstrained. They need this index.
            models.Index(fields=["trading_date"], name="idx_price_trading_date"),
        ]

    def __str__(self):
        return f"{self.instrument_id} @ {self.trading_date}"
