"""Serializers for analytics snapshots and computed metrics.

Two families. :class:`AnalyticSnapshotSerializer` reads *stored* rows; everything
after it reads the frozen *dataclasses* the engine returns from a computation.
The engine is the only implementation of each metric, so a serializer here names
fields and keeps the arithmetic out of the HTTP layer -- it never recomputes a
value, and every derived count (``undefined``, ``total``, ``windows``,
``unranked``) is read from the result rather than derived a second time.
"""

from rest_framework import serializers

from .models import AnalyticSnapshot


class DecimalStringField(serializers.CharField):
    """A ``Decimal`` rendered at the store's width: fixed point, eight places.

    Eight places is the precision the engine reports money and ratios at -- the
    width of ``AnalyticSnapshot.value`` -- so a ratio computed now and a price
    stored earlier render the same way. Padding is why this field exists rather
    than echoing ``str``: a close is whatever the driver returned
    (``Decimal('104')`` from SQLite, ``Decimal('104.00000000')`` from MySQL), and
    a payload must not depend on which database answered it. Padding also keeps a
    quantized zero off the ``0E-8`` exponent form.
    """

    def to_representation(self, value):
        return f"{value:.8f}"


class CountStringField(serializers.CharField):
    """A ``Decimal`` count rendered whole: ``1000``, not ``1000.00000000``.

    Trading volume is a share count, so eight places would report a precision the
    number does not have. This is the engine's ``format_count``.
    """

    def to_representation(self, value):
        return f"{value:.0f}"


class QuantizedStringField(serializers.CharField):
    """A ``Decimal`` rendered exactly as the engine quantized it.

    For a value whose precision is decided by *what was measured* rather than by
    the field: a ranking's ``value`` is a return at eight places under one measure
    and a whole volume under another, and the engine has already quantized it for
    the measure it used. Echoing the value leaves that decision in one place --
    padding would invent precision for a volume, truncating would lose the ratio's.
    """

    def to_representation(self, value):
        return format(value, "f")


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


# --------------------------------------------------------------------------- #
# Computed metrics: the engine's dataclasses, named
#
# Each field reads an attribute -- or a property -- of a frozen dataclass in
# ``services`` (the result envelope) or ``metrics`` (the points). DRF resolves a
# property, so a derived number is read from the result that already computed it
# rather than recomputed here: one definition, and no way for the serializer and
# the engine to disagree. Every field is ``read_only``: the request supplies
# *parameters*, never data.
# --------------------------------------------------------------------------- #


class MetricResultSerializer(serializers.Serializer):
    """The fields every computed metric shares, before its own points."""

    symbol = serializers.CharField(read_only=True)
    backend = serializers.CharField(read_only=True)
    start_date = serializers.DateField(read_only=True)
    end_date = serializers.DateField(read_only=True)
    padding = serializers.IntegerField(read_only=True)
    undefined = serializers.IntegerField(read_only=True)


class ReturnPointSerializer(serializers.Serializer):
    trading_date = serializers.DateField(read_only=True)
    close = DecimalStringField(read_only=True)
    previous_close = DecimalStringField(read_only=True)
    value = DecimalStringField(read_only=True)


class DailyReturnsSerializer(MetricResultSerializer):
    points = ReturnPointSerializer(many=True, read_only=True)


class CumulativePointSerializer(serializers.Serializer):
    trading_date = serializers.DateField(read_only=True)
    close = DecimalStringField(read_only=True)
    base_close = DecimalStringField(read_only=True)
    growth = DecimalStringField(read_only=True)
    value = DecimalStringField(read_only=True)


class CumulativeReturnsSerializer(MetricResultSerializer):
    #: The range's return by its last observation -- ``None`` if the chain broke.
    total = DecimalStringField(read_only=True)
    points = CumulativePointSerializer(many=True, read_only=True)


class MovingAveragePointSerializer(serializers.Serializer):
    trading_date = serializers.DateField(read_only=True)
    close = DecimalStringField(read_only=True)
    value = DecimalStringField(read_only=True)
    window = serializers.IntegerField(read_only=True)
    sample_size = serializers.IntegerField(read_only=True)


class NamedMovingAverageSerializer(serializers.Serializer):
    name = serializers.CharField(read_only=True)
    window = serializers.IntegerField(read_only=True)
    points = MovingAveragePointSerializer(many=True, read_only=True)


class MovingAveragesSerializer(MetricResultSerializer):
    #: The windows computed, in the conventional order.
    windows = serializers.ListField(child=serializers.IntegerField(), read_only=True)
    series = NamedMovingAverageSerializer(many=True, read_only=True)


class VolatilityPointSerializer(serializers.Serializer):
    trading_date = serializers.DateField(read_only=True)
    value = DecimalStringField(read_only=True)
    daily = DecimalStringField(read_only=True)
    mean = DecimalStringField(read_only=True)
    window = serializers.IntegerField(read_only=True)
    sample_size = serializers.IntegerField(read_only=True)


class RollingVolatilitySerializer(MetricResultSerializer):
    window = serializers.IntegerField(read_only=True)
    periods_per_year = serializers.IntegerField(read_only=True)
    points = VolatilityPointSerializer(many=True, read_only=True)


class DrawdownPointSerializer(serializers.Serializer):
    trading_date = serializers.DateField(read_only=True)
    close = DecimalStringField(read_only=True)
    peak = DecimalStringField(read_only=True)
    peak_date = serializers.DateField(read_only=True)
    value = DecimalStringField(read_only=True)


class DrawdownSummarySerializer(serializers.Serializer):
    value = DecimalStringField(read_only=True)
    peak_date = serializers.DateField(read_only=True)
    trough_date = serializers.DateField(read_only=True)
    peak_close = DecimalStringField(read_only=True)
    trough_close = DecimalStringField(read_only=True)
    recovered = serializers.BooleanField(read_only=True)


class MaximumDrawdownSerializer(MetricResultSerializer):
    summary = DrawdownSummarySerializer(read_only=True)
    points = DrawdownPointSerializer(many=True, read_only=True)


class ExtremePointSerializer(serializers.Serializer):
    trading_date = serializers.DateField(read_only=True)
    close = DecimalStringField(read_only=True)
    highest_close = DecimalStringField(read_only=True)
    highest_close_date = serializers.DateField(read_only=True)
    lowest_close = DecimalStringField(read_only=True)
    lowest_close_date = serializers.DateField(read_only=True)


class ExtremesSummarySerializer(serializers.Serializer):
    highest_close = DecimalStringField(read_only=True)
    highest_close_date = serializers.DateField(read_only=True)
    lowest_close = DecimalStringField(read_only=True)
    lowest_close_date = serializers.DateField(read_only=True)
    highest_high = DecimalStringField(read_only=True)
    highest_high_date = serializers.DateField(read_only=True)
    lowest_low = DecimalStringField(read_only=True)
    lowest_low_date = serializers.DateField(read_only=True)
    traded_range = DecimalStringField(read_only=True)


class PriceExtremesSerializer(MetricResultSerializer):
    summary = ExtremesSummarySerializer(read_only=True)
    points = ExtremePointSerializer(many=True, read_only=True)


class VolumePointSerializer(serializers.Serializer):
    trading_date = serializers.DateField(read_only=True)
    #: A share count, so whole; ``relative_volume`` is a ratio, so eight places.
    volume = CountStringField(read_only=True)
    cumulative_volume = CountStringField(read_only=True)
    relative_volume = DecimalStringField(read_only=True)


class VolumeSummarySerializer(serializers.Serializer):
    total_volume = CountStringField(read_only=True)
    average_volume = CountStringField(read_only=True)
    observations = serializers.IntegerField(read_only=True)
    highest_volume = CountStringField(read_only=True)
    highest_volume_date = serializers.DateField(read_only=True)
    lowest_volume = CountStringField(read_only=True)
    lowest_volume_date = serializers.DateField(read_only=True)
    sessions_above_average = serializers.IntegerField(read_only=True)
    sessions_at_average = serializers.IntegerField(read_only=True)
    sessions_below_average = serializers.IntegerField(read_only=True)


class VolumeSummariesSerializer(MetricResultSerializer):
    summary = VolumeSummarySerializer(read_only=True)
    points = VolumePointSerializer(many=True, read_only=True)


class PeriodChangeSerializer(serializers.Serializer):
    label = serializers.CharField(read_only=True)
    base_date = serializers.DateField(read_only=True)
    base_close = DecimalStringField(read_only=True)
    end_date = serializers.DateField(read_only=True)
    end_close = DecimalStringField(read_only=True)
    change = DecimalStringField(read_only=True)
    #: Derived from ``change`` rather than recomputed, so the two cannot disagree.
    percent_change = DecimalStringField(read_only=True)
    observations = serializers.IntegerField(read_only=True)


class PercentageChangeSerializer(MetricResultSerializer):
    periods = PeriodChangeSerializer(many=True, read_only=True)


class RankingRowSerializer(serializers.Serializer):
    #: ``null`` for an instrument the measure could not be computed for; such rows
    #: are listed after the ranked ones rather than dropped.
    rank = serializers.IntegerField(read_only=True)
    instrument_id = serializers.IntegerField(read_only=True)
    symbol = serializers.CharField(read_only=True)
    #: The measure's own precision: eight places for a return, whole for a volume.
    value = QuantizedStringField(read_only=True)
    observations = serializers.IntegerField(read_only=True)
    first_date = serializers.DateField(read_only=True)
    last_date = serializers.DateField(read_only=True)


class RankingSerializer(serializers.Serializer):
    """A ranking is *about* symbols, so it has no ``symbol`` of its own."""

    measure = serializers.CharField(read_only=True)
    backend = serializers.CharField(read_only=True)
    descending = serializers.BooleanField(read_only=True)
    start_date = serializers.DateField(read_only=True)
    end_date = serializers.DateField(read_only=True)
    lookback = serializers.IntegerField(read_only=True)
    universe = serializers.IntegerField(read_only=True)
    ranked = serializers.IntegerField(read_only=True)
    unranked = serializers.IntegerField(read_only=True)
    truncated = serializers.IntegerField(read_only=True)
    rows = RankingRowSerializer(many=True, read_only=True)
