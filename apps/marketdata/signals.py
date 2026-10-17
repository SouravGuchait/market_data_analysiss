"""Cache invalidation for row-at-a-time price writes.

The ingestion pipeline writes prices with ``bulk_create``, which emits **no
signals at all**, so it invalidates explicitly after its transaction commits --
see ``apps.ingestion.etl.persistence.upsert_prices``. These receivers cover the
writes that do go row by row: the admin, a management command, a shell session.

Both routes bump the same namespace version, so a cached usage summary cannot
outlive the rows underneath it however those rows arrived.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.common.cache import invalidate_after_commit

from .models import PriceRecord


@receiver(post_save, sender=PriceRecord, dispatch_uid="marketdata.invalidate_on_save")
@receiver(post_delete, sender=PriceRecord, dispatch_uid="marketdata.invalidate_on_delete")
def invalidate_usage_summaries(sender, **kwargs):
    """Prices are what the usage summary counts, so a price invalidates it."""
    invalidate_after_commit("usage")
