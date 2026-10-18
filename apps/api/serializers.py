"""Serializers for the public API facade.

Only representations that have no home in a domain app live here; everything
else is imported from the app that owns it, so a field is defined once.
"""

from rest_framework import serializers

from apps.analytics.serializers import DecimalStringField


class InstrumentQualitySerializer(serializers.Serializer):
    """The data-quality review of one instrument's window.

    ``coverage`` and ``missing_sessions`` are read from the result's own
    properties rather than recomputed, so the ratio and the list it is derived
    from cannot disagree.
    """

    symbol = serializers.CharField(read_only=True)
    start_date = serializers.DateField(read_only=True)
    end_date = serializers.DateField(read_only=True)
    observations = serializers.IntegerField(read_only=True)
    sessions = serializers.IntegerField(read_only=True)
    missing_sessions = serializers.IntegerField(read_only=True)
    coverage = DecimalStringField(read_only=True)
    first_date = serializers.DateField(read_only=True)
    last_date = serializers.DateField(read_only=True)
    missing_dates = serializers.ListField(child=serializers.DateField(), read_only=True)
