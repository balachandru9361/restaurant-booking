from django.apps import AppConfig


class StoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "store"

    def ready(self):
        # Auto stock deduction is OFF. To turn it on again, uncomment the next line.
        # from . import signals  # noqa
        pass