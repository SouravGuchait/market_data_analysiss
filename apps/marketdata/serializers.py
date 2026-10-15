"""Serializers for price records."""

from rest_framework import serializers

from .models import PriceRecord


class PriceRecordSerializer(serializers.ModelSerializer):
    """Full OHLC representation.

    ``instrument`` and ``ingestion_job`` are denormalised to their identifiers
    rather than nested so a page of prices stays small and needs no extra query.
    """

    class Meta:
        model = PriceRecord
        fields = [
            "trading_date",
            "open",
            "high",
            "low",
            "close",
            "adjusted_close",
            "volume",
            "source",
        ]
        # Prices are written by the ingestion pipeline, never by this API.
        read_only_fields = fields


class PriceRecordLatestSerializer(serializers.ModelSerializer):
    """A single day's close, for the "latest price" read."""

    class Meta:
        model = PriceRecord
        fields = ["trading_date", "close", "volume", "source"]
        read_only_fields = fields
