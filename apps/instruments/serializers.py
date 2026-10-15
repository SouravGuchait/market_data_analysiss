"""Serializers for instruments and exchanges."""

from rest_framework import serializers

from .models import Exchange, Instrument


class ExchangeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Exchange
        fields = ["id", "code", "name", "timezone"]


class InstrumentListSerializer(serializers.ModelSerializer):
    """Flat representation used for list pages.

    The exchange is flattened to its code rather than nested, so a page of
    instruments needs one query and returns less JSON. Payload size matters when
    the table holds thousands of rows.
    """

    exchange_code = serializers.CharField(source="exchange.code", read_only=True)

    class Meta:
        model = Instrument
        fields = [
            "id",
            "symbol",
            "name",
            "asset_type",
            "exchange_code",
            "currency",
            "is_active",
        ]


class InstrumentDetailSerializer(serializers.ModelSerializer):
    """Full representation used for a single instrument."""

    exchange = ExchangeSerializer(read_only=True)

    class Meta:
        model = Instrument
        fields = [
            "id",
            "symbol",
            "name",
            "asset_type",
            "exchange",
            "currency",
            "is_active",
            "created_at",
            "updated_at",
        ]
