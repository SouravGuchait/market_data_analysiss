from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'apps.analytics'

    def ready(self):
        # Registers the cache-invalidation receivers. Imported here rather than
        # at module level so the models are loaded by the time they connect.
        from . import signals  # noqa: F401
