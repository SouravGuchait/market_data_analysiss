from django.apps import AppConfig


class MarketdataConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.marketdata"

    def ready(self):
        # Registers the cache-invalidation receivers for row-at-a-time price
        # writes. See the module docstring for why bulk writes are not covered.
        from . import signals  # noqa: F401
