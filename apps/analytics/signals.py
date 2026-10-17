"""Cache invalidation for snapshot writes.

Snapshots have no pipeline of their own yet -- today they arrive through the
admin or a script. That is precisely why this is a signal rather than a call
buried in a writer: whoever adds the writer gets invalidation for free and cannot
forget it, and the admin is covered from the moment the first row is saved.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.common.cache import invalidate_after_commit

from .models import AnalyticSnapshot


@receiver(post_save, sender=AnalyticSnapshot, dispatch_uid="analytics.invalidate_on_save")
@receiver(post_delete, sender=AnalyticSnapshot, dispatch_uid="analytics.invalidate_on_delete")
def invalidate_metric_pages(sender, **kwargs):
    """Orphan every cached metrics page once the write commits.

    ``on_commit`` rather than an immediate bump: a snapshot written inside a
    transaction that then rolls back was never visible, so invalidating for it
    would throw away a good entry and force a recompute that returns the same
    answer.
    """
    invalidate_after_commit("analytics")
