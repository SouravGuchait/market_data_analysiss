"""Project configuration package.

Importing the Celery app here is what makes ``@shared_task`` work: Django loads
this package before the app registry is populated, so the shared application is
registered by the time any task module is imported.
"""

from .celery import app as celery_app

__all__ = ("celery_app",)
