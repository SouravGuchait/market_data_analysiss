"""Models shared by every domain app.

Concrete domain models live in their own apps; this module only defines the
abstract bases they inherit from.
"""

from django.conf import settings
from django.db import models


class OwnedModel(models.Model):
    """Abstract base for rows that only their creator may read or write."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s",
    )

    class Meta:
        abstract = True
