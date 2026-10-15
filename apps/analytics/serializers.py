"""Serializers for analytics snapshots."""

from rest_framework import serializers

from .models import AnalyticSnapshot


class AnalyticSnapshotSerializer(serializers.ModelSerializer):
    """Output representation of one metric.

    The instrument is flattened to its symbol, which is the only part a metric
    row needs, so no extra query and a smaller payload.
    """

    symbol = serializers.CharField(source="instrument.symbol", read_only=True)

    class Meta:
        model = AnalyticSnapshot
        fields = [
            "symbol",
            "metric_type",
            "as_of_date",
            "lookback_days",
            "value",
            "metadata",
            "calculated_at",
        ]
        # Snapshots are written by the ingestion pipeline, never by this API.
        read_only_fields = fields
