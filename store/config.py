"""Coupon programme configuration: every N-th successfully placed order unlocks one coupon
worth X% off.  Read at call time so tests (and deployments) can override via settings."""
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

DEFAULT_EVERY_N_ORDERS = 5
DEFAULT_DISCOUNT_PERCENT = 10


def coupon_config():
    n = getattr(settings, "COUPON_EVERY_N_ORDERS", DEFAULT_EVERY_N_ORDERS)
    x = getattr(settings, "COUPON_DISCOUNT_PERCENT", DEFAULT_DISCOUNT_PERCENT)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ImproperlyConfigured("COUPON_EVERY_N_ORDERS must be an integer >= 1")
    if isinstance(x, bool) or not isinstance(x, int) or not (1 <= x <= 100):
        raise ImproperlyConfigured("COUPON_DISCOUNT_PERCENT must be an integer between 1 and 100")
    return n, x