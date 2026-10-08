import threading

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.db.models.deletion import ProtectedError

from store.models import Cart, Coupon, Order
from datetime import timedelta
from django.utils import timezone

pytestmark = pytest.mark.django_db


# ---------------- helpers ----------------

def make_order(cart=None, subtotal=100000, discount=0, coupon=None, percent=None, **kw):
    cart = cart or Cart.objects.create()
    return Order.objects.create(
        cart=cart,
        subtotal=subtotal,
        discount_amount=discount,
        total=subtotal - discount,
        coupon=coupon,
        coupon_code=coupon.code if coupon else "",
        discount_percent=percent,
        **kw,
    )


@pytest.fixture
def coupon():
    return Coupon.objects.create(code="SAVE-TEST", milestone=5, discount_percent=10)


# ---------------- defaults and basics ----------------

def test_defaults_on_new_order():
    o = make_order()
    assert o.id is not None
    assert o.created_at is not None
    assert o.coupon is None
    assert o.coupon_code == ""
    assert o.discount_percent is None
    assert o.idempotency_key is None
    assert o.payment_reference == ""
    assert o.discount_amount == 0
    assert o.total == o.subtotal


def test_amounts_round_trip_as_integers():
    o = make_order(subtotal=12345)
    o.refresh_from_db()
    assert (o.subtotal, o.discount_amount, o.total) == (12345, 0, 12345)
    assert all(isinstance(v, int) for v in (o.subtotal, o.discount_amount, o.total))


def test_str_contains_id_and_total():
    o = make_order(subtotal=500)
    assert str(o.id) in str(o)
    assert "total=500" in str(o)



def test_default_ordering_is_newest_first():
    first = make_order()
    second = make_order()
    third = make_order()

   
    now = timezone.now()
    for order, age in ((first, 3), (second, 2), (third, 1)):
        Order.objects.filter(pk=order.pk).update(
            created_at=now - timedelta(minutes=age)
        )

    assert list(Order.objects.all()) == [third, second, first]


# ---------------- coupon snapshot ----------------

def test_order_with_coupon_stores_snapshot(coupon):
    o = make_order(subtotal=100000, discount=10000, coupon=coupon, percent=10)
    o.refresh_from_db()
    assert o.coupon_id == coupon.id
    assert o.coupon_code == "SAVE-TEST"
    assert o.discount_percent == 10
    assert (o.discount_amount, o.total) == (10000, 90000)
    assert coupon.order == o


def test_snapshot_survives_later_coupon_changes(coupon):
    o = make_order(subtotal=100000, discount=10000, coupon=coupon, percent=10)
    Coupon.objects.filter(pk=coupon.pk).update(discount_percent=50)
    o.refresh_from_db()
    assert o.discount_percent == 10
    assert o.discount_amount == 10000


def test_hundred_percent_discount_gives_zero_total(coupon):
    o = make_order(subtotal=5000, discount=5000, coupon=coupon, percent=100)
    assert o.total == 0


def test_coupon_with_zero_discount_is_allowed(coupon):
    # tiny subtotal: floor(1 * 10 / 100) == 0, coupon still redeemed
    o = make_order(subtotal=1, discount=0, coupon=coupon, percent=10)
    assert (o.discount_amount, o.total) == (0, 1)


# ---------------- uniqueness ----------------

def test_second_order_for_same_cart_is_rejected():
    cart = Cart.objects.create()
    make_order(cart=cart)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_order(cart=cart)
    assert Order.objects.filter(cart=cart).count() == 1


def test_different_carts_can_each_have_an_order():
    make_order()
    make_order()
    assert Order.objects.count() == 2


def test_duplicate_idempotency_key_is_rejected():
    make_order(idempotency_key="key-1")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_order(idempotency_key="key-1")
    assert Order.objects.count() == 1


def test_distinct_idempotency_keys_are_allowed():
    make_order(idempotency_key="key-1")
    make_order(idempotency_key="key-2")
    assert Order.objects.count() == 2


def test_many_orders_with_null_idempotency_key_are_allowed():
    make_order()
    make_order()
    make_order()
    assert Order.objects.filter(idempotency_key__isnull=True).count() == 3


def test_blank_string_idempotency_keys_collide():
    """'' is a real value, not NULL. The service must store None when the
    client sends no key, or the second keyless order will fail."""
    make_order(idempotency_key="")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_order(idempotency_key="")


def test_same_coupon_on_two_orders_is_rejected(coupon):
    make_order(subtotal=1000, discount=100, coupon=coupon, percent=10)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_order(subtotal=2000, discount=200, coupon=coupon, percent=10)
    assert Order.objects.filter(coupon=coupon).count() == 1


def test_many_orders_without_coupon_are_allowed():
    for _ in range(3):
        make_order()
    assert Order.objects.filter(coupon__isnull=True).count() == 3


# ---------------- check constraints ----------------

def test_total_must_equal_subtotal_minus_discount(coupon):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Order.objects.create(
                cart=Cart.objects.create(), coupon=coupon, coupon_code=coupon.code,
                discount_percent=10, subtotal=1000, discount_amount=100, total=1000,
            )


def test_total_cannot_be_inflated_without_coupon():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Order.objects.create(
                cart=Cart.objects.create(), subtotal=1000, discount_amount=0, total=1100,
            )


def test_discount_cannot_exceed_subtotal(coupon):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Order.objects.create(
                cart=Cart.objects.create(), coupon=coupon, coupon_code=coupon.code,
                discount_percent=100, subtotal=1000, discount_amount=1500, total=0,
            )


def test_discount_without_coupon_is_rejected():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Order.objects.create(
                cart=Cart.objects.create(), subtotal=1000, discount_amount=100, total=900,
            )


def test_discount_percent_without_coupon_is_rejected():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Order.objects.create(
                cart=Cart.objects.create(), subtotal=1000, discount_amount=0,
                total=1000, discount_percent=10,
            )


def test_coupon_without_discount_percent_snapshot_is_rejected(coupon):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Order.objects.create(
                cart=Cart.objects.create(), coupon=coupon, coupon_code=coupon.code,
                discount_percent=None, subtotal=1000, discount_amount=0, total=1000,
            )


@pytest.mark.parametrize("field", ["subtotal", "discount_amount", "total"])
def test_negative_amounts_are_rejected(field):
    values = {"subtotal": 0, "discount_amount": 0, "total": 0}
    values[field] = -1
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Order.objects.create(cart=Cart.objects.create(), **values)


def test_update_that_breaks_reconciliation_is_rejected():
    o = make_order(subtotal=1000)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Order.objects.filter(pk=o.pk).update(total=1)
    o.refresh_from_db()
    assert o.total == 1000


def test_required_fields_cannot_be_null():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Order.objects.create(cart=Cart.objects.create(), subtotal=None, total=0)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Order.objects.create(cart=Cart.objects.create(), subtotal=0, total=None)


# ---------------- full_clean (Django 4.1+ validates constraints) ----------------

def test_full_clean_accepts_valid_order():
    o = Order(cart=Cart.objects.create(), subtotal=1000, discount_amount=0, total=1000)
    o.full_clean()


def test_full_clean_rejects_duplicate_cart():
    cart = Cart.objects.create()
    make_order(cart=cart)
    dup = Order(cart=cart, subtotal=1000, discount_amount=0, total=1000)
    with pytest.raises(ValidationError):
        dup.full_clean()


def test_full_clean_rejects_unreconciled_total():
    o = Order(cart=Cart.objects.create(), subtotal=1000, discount_amount=0, total=999)
    with pytest.raises(ValidationError):
        o.full_clean()


def test_full_clean_rejects_overlong_coupon_code():
    o = Order(
        cart=Cart.objects.create(), subtotal=1000, discount_amount=0,
        total=1000, coupon_code="X" * 33,
    )
    with pytest.raises(ValidationError) as exc:
        o.full_clean()
    assert "coupon_code" in exc.value.message_dict


# ---------------- delete behaviour ----------------

def test_cart_with_order_cannot_be_deleted():
    o = make_order()
    with pytest.raises(ProtectedError):
        o.cart.delete()
    assert Cart.objects.filter(pk=o.cart_id).exists()


def test_coupon_used_by_order_cannot_be_deleted(coupon):
    make_order(subtotal=1000, discount=100, coupon=coupon, percent=10)
    with pytest.raises(ProtectedError):
        coupon.delete()


def test_deleting_order_leaves_cart_and_coupon(coupon):
    o = make_order(subtotal=1000, discount=100, coupon=coupon, percent=10)
    cart_id = o.cart_id
    o.delete()
    assert Cart.objects.filter(pk=cart_id).exists()
    assert Coupon.objects.filter(pk=coupon.pk).exists()


# ---------------- competing operations (real Postgres, real threads) ----------------

def _run_parallel(worker, n):
    barrier = threading.Barrier(n)
    results = []

    def target():
        try:
            barrier.wait()
            results.append(worker())
        except Exception as e:                      # noqa: BLE001
            results.append(e)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=target) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.mark.django_db(transaction=True)
def test_concurrent_orders_for_same_cart_only_one_survives():
    cart = Cart.objects.create()

    def place():
        try:
            with transaction.atomic():
                Order.objects.create(cart=cart, subtotal=1000, total=1000)
            return "ok"
        except IntegrityError:
            return "conflict"

    results = _run_parallel(place, 8)

    assert results.count("ok") == 1
    assert results.count("conflict") == 7
    assert Order.objects.filter(cart=cart).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_orders_with_same_coupon_only_one_survives():
    coupon = Coupon.objects.create(code="RACE", milestone=5, discount_percent=10)

    def place():
        cart = Cart.objects.create()
        try:
            with transaction.atomic():
                Order.objects.create(
                    cart=cart, coupon=coupon, coupon_code="RACE", discount_percent=10,
                    subtotal=1000, discount_amount=100, total=900,
                )
            return "ok"
        except IntegrityError:
            return "conflict"

    results = _run_parallel(place, 8)

    assert results.count("ok") == 1
    assert Order.objects.filter(coupon=coupon).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_orders_with_same_idempotency_key_only_one_survives():
    def place():
        cart = Cart.objects.create()
        try:
            with transaction.atomic():
                Order.objects.create(
                    cart=cart, idempotency_key="retry-1", subtotal=1000, total=1000
                )
            return "ok"
        except IntegrityError:
            return "conflict"

    results = _run_parallel(place, 8)

    assert results.count("ok") == 1
    assert Order.objects.filter(idempotency_key="retry-1").count() == 1