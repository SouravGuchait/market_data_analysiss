"""Invalidate shared reference-data reads after instrument/exchange writes."""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.common.cache import invalidate_after_commit

from .models import Exchange, Instrument


@receiver(post_save, sender=Instrument, dispatch_uid="instruments.invalidate_on_save")
@receiver(post_delete, sender=Instrument, dispatch_uid="instruments.invalidate_on_delete")
@receiver(post_save, sender=Exchange, dispatch_uid="exchanges.invalidate_on_save")
@receiver(post_delete, sender=Exchange, dispatch_uid="exchanges.invalidate_on_delete")
def invalidate_reference_reads(sender, **kwargs):
    invalidate_after_commit("reference")
