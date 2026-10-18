from django.apps import AppConfig

class InstrumentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.instruments"

    def ready(self):
        from . import signals  # noqa: F401
