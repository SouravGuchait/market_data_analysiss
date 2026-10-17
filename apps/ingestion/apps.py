from django.apps import AppConfig


class IngestionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'apps.ingestion'

    def ready(self):
        # Registers the cache-invalidation receiver that watches job state.
        from . import signals  # noqa: F401
