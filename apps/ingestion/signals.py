"""Cache invalidation for job writes.

The usage summary counts a user's jobs by status and sums their row counters, so
a job changing state makes a cached summary wrong -- but a *running* job writes
that row constantly, and none of those writes change the summary. Invalidating on
each one would evict the entry several times a minute and turn the cache into a
no-op, so progress-only writes are skipped and the TTL covers what they do touch.

The guard keys off ``update_fields``, which is what makes this precise: the two
saves that matter (the claim, which moves the job to PROCESSING, and the final
save, which sets its status and totals) both name ``status``, and every periodic
progress save is confined to the set below.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.common.cache import invalidate_after_commit

from .models import IngestionJob

#: The fields a running job writes on its progress cadence. A save limited to
#: these leaves the summary's inputs untouched: it groups by ``status`` and sums
#: ``rows_received``, neither of which appears here.
PROGRESS_ONLY_FIELDS = frozenset(
    {"checkpoint", "rows_processed", "rows_accepted", "rows_rejected", "heartbeat_at"}
)


@receiver(post_save, sender=IngestionJob, dispatch_uid="ingestion.invalidate_usage")
def invalidate_usage_on_job_change(sender, instance, update_fields=None, **kwargs):
    if update_fields and set(update_fields) <= PROGRESS_ONLY_FIELDS:
        # A progress tick. Skipping it is the difference between a cache and a
        # cache-shaped hole that every heartbeat writes through.
        return
    invalidate_after_commit("usage")
