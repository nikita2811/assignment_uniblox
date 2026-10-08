from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class StoreConfig(AppConfig):
    name = 'store'
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        n = settings.REWARD_EVERY_N_ORDERS
        x = settings.REWARD_DISCOUNT_PERCENT
        if not (isinstance(n, int) and n >= 1):
            raise ImproperlyConfigured("REWARD_EVERY_N_ORDERS must be an integer >= 1")
        if not (isinstance(x, int) and 1 <= x <= 100):
            raise ImproperlyConfigured("REWARD_DISCOUNT_PERCENT must be an integer in 1..100")
