"""Analytics snapshots: one calculated metric per instrument, date, and lookback."""

from django.db import models

from apps.instruments.models import Instrument


class MetricType(models.TextChoices):
    RETURN = "return", "Return"
    MOVING_AVERAGE = "moving_average", "Moving Average"
    VOLATILITY = "volatility", "Volatility"
    DRAWDOWN = "drawdown", "Drawdown"


class AnalyticSnapshot(models.Model):
    """A metric value computed as of a date over a lookback window.

    Snapshots are written rather than recomputed on read, so the same question
    always returns the same answer and the expensive calculation is auditable.
    """

    instrument = models.ForeignKey(
        Instrument, on_delete=models.CASCADE, related_name="snapshots"
    )
    as_of_date = models.DateField()
    metric_type = models.CharField(max_length=32, choices=MetricType.choices)
    lookback_days = models.PositiveIntegerField()
    value = models.DecimalField(max_digits=18, decimal_places=8)
    metadata = models.JSONField(null=True, blank=True)
    calculated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-as_of_date", "metric_type"]
        constraints = [
            # The grain of the table: one row per instrument, metric, date, and
            # lookback. Recalculating the same metric is an upsert, not a new row.
            models.UniqueConstraint(
                fields=["instrument", "as_of_date", "metric_type", "lookback_days"],
                name="uniq_snapshot_grain",
            ),
        ]
        indexes = [
            # "Latest metric of this type" is the common read.
            models.Index(
                fields=["instrument", "metric_type", "-as_of_date"],
                name="idx_snapshot_instr_metric_date",
            ),
        ]

    def __str__(self):
        return f"{self.instrument_id} {self.metric_type} as of {self.as_of_date}"
