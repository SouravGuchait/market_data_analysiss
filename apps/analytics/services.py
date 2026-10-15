"""Business logic for analytics snapshots."""

from .models import AnalyticSnapshot


def get_snapshot_queryset():
    """The base queryset for every analytics read.

    ``select_related`` joins the instrument, because every representation shows
    which symbol a metric belongs to.
    """
    return AnalyticSnapshot.objects.select_related("instrument")


def list_snapshots_for_instrument(
    instrument,
    *,
    metric_type=None,
    start_date=None,
    end_date=None,
    lookback_days=None,
):
    """Returns snapshots for one instrument, newest date first.

    All arguments are optional and additive.
    """
    queryset = get_snapshot_queryset().filter(instrument=instrument)

    if metric_type:
        queryset = queryset.filter(metric_type=metric_type)
    if lookback_days is not None:
        queryset = queryset.filter(lookback_days=lookback_days)
    if start_date:
        queryset = queryset.filter(as_of_date__gte=start_date)
    if end_date:
        queryset = queryset.filter(as_of_date__lte=end_date)

    return queryset.order_by("-as_of_date", "metric_type")


def get_latest_snapshot(instrument, metric_type, *, lookback_days=None):
    """Returns the most recent snapshot of one metric, or None.

    Relies on ``(instrument, metric_type, as_of_date)`` being indexed, so this is
    an index lookup rather than a scan and sort.
    """
    queryset = get_snapshot_queryset().filter(
        instrument=instrument, metric_type=metric_type
    )
    if lookback_days is not None:
        queryset = queryset.filter(lookback_days=lookback_days)

    return queryset.order_by("-as_of_date").first()
