import threading

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.db.models import F
from django.db.models.deletion import ProtectedError

from store.models import Product,Cart, CartItem


pytestmark = pytest.mark.django_db


@pytest.fixture
def cart():
    return Cart.objects.create()


@pytest.fixture
def rice():
    return Product.objects.create(
        name="Basmati Rice", sku="RICE-5KG", product_qty="5kg",
        unit_price=59900, inventory=100,
    )


@pytest.fixture
def dal():
    return Product.objects.create(
        name="Toor Dal", sku="DAL-1KG", product_qty="1kg",
        unit_price=16500, inventory=200,
    )


# ---------------- basic creation / relations ----------------

def test_create_cart_item(cart, rice):
    item = CartItem.objects.create(cart=cart, product=rice, quantity=2)
    item.refresh_from_db()
    assert item.cart_id == cart.id
    assert item.product_id == rice.id
    assert item.quantity == 2


def test_related_name_items_on_cart(cart, rice, dal):
    CartItem.objects.create(cart=cart, product=rice, quantity=1)
    CartItem.objects.create(cart=cart, product=dal, quantity=3)
    assert cart.items.count() == 2
    assert {i.product_id for i in cart.items.all()} == {rice.id, dal.id}


def test_quantity_is_required(cart, rice):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CartItem.objects.create(cart=cart, product=rice, quantity=None)


# ---------------- unique (cart, product) ----------------

def test_same_product_twice_in_same_cart_violates_unique_constraint(cart, rice):
    CartItem.objects.create(cart=cart, product=rice, quantity=1)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CartItem.objects.create(cart=cart, product=rice, quantity=2)
    # original row untouched
    assert CartItem.objects.get(cart=cart, product=rice).quantity == 1


def test_same_product_in_different_carts_is_allowed(rice):
    c1, c2 = Cart.objects.create(), Cart.objects.create()
    CartItem.objects.create(cart=c1, product=rice, quantity=1)
    CartItem.objects.create(cart=c2, product=rice, quantity=1)
    assert CartItem.objects.filter(product=rice).count() == 2


def test_different_products_in_same_cart_is_allowed(cart, rice, dal):
    CartItem.objects.create(cart=cart, product=rice, quantity=1)
    CartItem.objects.create(cart=cart, product=dal, quantity=1)
    assert cart.items.count() == 2


# ---------------- quantity > 0 ----------------

@pytest.mark.parametrize("qty", [0, -1, -100])
def test_non_positive_quantity_violates_check_constraint(cart, rice, qty):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CartItem.objects.create(cart=cart, product=rice, quantity=qty)
    assert CartItem.objects.count() == 0


def test_update_to_zero_violates_check_constraint(cart, rice):
    item = CartItem.objects.create(cart=cart, product=rice, quantity=2)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CartItem.objects.filter(pk=item.pk).update(quantity=0)
    item.refresh_from_db()
    assert item.quantity == 2


def test_full_clean_rejects_non_positive_quantity(cart, rice):
    item = CartItem(cart=cart, product=rice, quantity=0)
    with pytest.raises(ValidationError):
        item.full_clean()


def test_full_clean_rejects_duplicate_cart_product(cart, rice):
    CartItem.objects.create(cart=cart, product=rice, quantity=1)
    dup = CartItem(cart=cart, product=rice, quantity=2)
    with pytest.raises(ValidationError):
        dup.full_clean()


def test_full_clean_accepts_valid_item(cart, rice):
    CartItem(cart=cart, product=rice, quantity=1).full_clean()


# ---------------- delete behaviour ----------------

def test_deleting_cart_cascades_to_items(cart, rice, dal):
    CartItem.objects.create(cart=cart, product=rice, quantity=1)
    CartItem.objects.create(cart=cart, product=dal, quantity=1)
    cart.delete()
    assert CartItem.objects.count() == 0
    assert Product.objects.filter(pk__in=[rice.pk, dal.pk]).count() == 2


def test_deleting_product_in_a_cart_is_protected(cart, rice):
    CartItem.objects.create(cart=cart, product=rice, quantity=1)
    with pytest.raises(ProtectedError):
        rice.delete()
    assert Product.objects.filter(pk=rice.pk).exists()


def test_deleting_item_does_not_delete_cart_or_product(cart, rice):
    item = CartItem.objects.create(cart=cart, product=rice, quantity=1)
    item.delete()
    assert Cart.objects.filter(pk=cart.pk).exists()
    assert Product.objects.filter(pk=rice.pk).exists()


# ---------------- atomic quantity update pattern ----------------

def test_f_expression_increment_is_applied_in_db(cart, rice):
    item = CartItem.objects.create(cart=cart, product=rice, quantity=2)
    updated = CartItem.objects.filter(pk=item.pk).update(quantity=F("quantity") + 3)
    item.refresh_from_db()
    assert updated == 1
    assert item.quantity == 5


# ---------------- competing operations ----------------

@pytest.mark.django_db(transaction=True)
def test_concurrent_inserts_of_same_cart_product_only_one_row_survives():
    """The unique constraint must hold under real concurrency:
    exactly one insert wins, the rest raise IntegrityError."""
    cart = Cart.objects.create()
    product = Product.objects.create(
        name="Race Item", sku="RACE", unit_price=100, inventory=50,
    )
    workers = 8
    barrier = threading.Barrier(workers)
    results = []

    def insert():
        try:
            barrier.wait()
            with transaction.atomic():
                CartItem.objects.create(cart=cart, product=product, quantity=1)
            results.append("ok")
        except IntegrityError:
            results.append("conflict")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=insert) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1
    assert results.count("conflict") == workers - 1
    assert CartItem.objects.filter(cart=cart, product=product).count() == 1