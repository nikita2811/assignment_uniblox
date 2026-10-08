import threading

import pytest
from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError, connections, transaction
from django.db.models.deletion import ProtectedError

from store.models import Cart, Order, OrderItem, Product

pytestmark = pytest.mark.django_db


# ---------------- helpers / fixtures ----------------

def make_order(subtotal=0):
    return Order.objects.create(
        cart=Cart.objects.create(), subtotal=subtotal, discount_amount=0, total=subtotal
    )


def add_item(order, product, qty=1, **overrides):
    data = dict(
        order=order,
        product=product,
        sku=product.sku,
        product_name=product.name,
        product_qty=product.product_qty,
        unit_price=product.unit_price,
        quantity=qty,
        line_total=product.unit_price * qty,
    )
    data.update(overrides)
    return OrderItem.objects.create(**data)


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


# ---------------- creation and relations ----------------

def test_create_order_item_persists_all_fields(rice):
    order = make_order(subtotal=2 * 59900)
    item = add_item(order, rice, 2)
    item.refresh_from_db()

    assert item.order_id == order.id
    assert item.product_id == rice.id
    assert item.sku == "RICE-5KG"
    assert item.product_name == "Basmati Rice"
    assert item.product_qty == "5kg"
    assert item.unit_price == 59900
    assert item.quantity == 2
    assert item.line_total == 119800


def test_related_name_items_on_order(rice, dal):
    order = make_order(subtotal=59900 + 3 * 16500)
    add_item(order, rice, 1)
    add_item(order, dal, 3)

    assert order.items.count() == 2
    assert {i.product_id for i in order.items.all()} == {rice.id, dal.id}


def test_item_lines_sum_to_order_subtotal(rice, dal):
    order = make_order(subtotal=2 * 59900 + 3 * 16500)
    add_item(order, rice, 2)
    add_item(order, dal, 3)

    assert sum(i.line_total for i in order.items.all()) == order.subtotal


def test_sku_and_pack_size_may_be_blank(rice):
    order = make_order(subtotal=59900)
    item = add_item(order, rice, 1, sku="", product_qty="")
    item.refresh_from_db()
    assert item.sku == "" and item.product_qty == ""


def test_required_fields_cannot_be_null(rice):
    order = make_order(subtotal=0)
    for field in ("unit_price", "quantity", "line_total", "product_name"):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                add_item(order, rice, 1, **{field: None})


# ---------------- snapshot behaviour ----------------

def test_snapshot_survives_product_changes(rice):
    order = make_order(subtotal=59900)
    item = add_item(order, rice, 1)

    Product.objects.filter(pk=rice.pk).update(
        name="Renamed Rice", unit_price=99999, product_qty="10kg", sku="CHANGED"
    )
    item.refresh_from_db()

    assert item.product_name == "Basmati Rice"
    assert item.unit_price == 59900
    assert item.product_qty == "5kg"
    assert item.sku == "RICE-5KG"
    assert item.line_total == 59900


def test_snapshot_price_may_differ_from_current_product_price(rice):
    """Order records what was charged, not what the product costs now."""
    order = make_order(subtotal=2 * 50000)
    item = add_item(order, rice, 2, unit_price=50000, line_total=100000)
    assert item.unit_price != rice.unit_price
    assert item.line_total == 100000


# ---------------- uniqueness ----------------

def test_same_product_twice_in_one_order_is_rejected(rice):
    order = make_order(subtotal=119800)
    add_item(order, rice, 1)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            add_item(order, rice, 1)
    assert order.items.count() == 1


def test_same_product_in_different_orders_is_allowed(rice):
    add_item(make_order(subtotal=59900), rice, 1)
    add_item(make_order(subtotal=59900), rice, 1)
    assert OrderItem.objects.filter(product=rice).count() == 2


def test_different_products_in_same_order_are_allowed(rice, dal):
    order = make_order(subtotal=59900 + 16500)
    add_item(order, rice, 1)
    add_item(order, dal, 1)
    assert order.items.count() == 2


# ---------------- check constraints ----------------

@pytest.mark.parametrize("qty", [0])
def test_zero_quantity_is_rejected(rice, qty):
    order = make_order()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            add_item(order, rice, qty, line_total=0)


def test_negative_quantity_is_rejected(rice):
    order = make_order()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            add_item(order, rice, -1, line_total=0)


def test_negative_unit_price_or_line_total_is_rejected(rice):
    order = make_order()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            add_item(order, rice, 1, unit_price=-1, line_total=-1)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            add_item(order, rice, 1, line_total=-5)


@pytest.mark.parametrize("line_total", [0, 1, 59899, 59901, 119800])
def test_line_total_must_equal_unit_price_times_quantity(rice, line_total):
    order = make_order()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            add_item(order, rice, 1, line_total=line_total)
    assert OrderItem.objects.count() == 0


def test_update_that_breaks_line_total_is_rejected(rice):
    order = make_order(subtotal=2 * 59900)
    item = add_item(order, rice, 2)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            OrderItem.objects.filter(pk=item.pk).update(quantity=3)
    item.refresh_from_db()
    assert item.quantity == 2


def test_integer_overflow_is_a_database_error_not_silent(rice):
    """PositiveIntegerField is a 32-bit column. The service layer should cap
    quantities and totals so this never reaches the DB."""
    order = make_order()
    with pytest.raises(DataError):
        with transaction.atomic():
            add_item(order, rice, 1, unit_price=2**31, line_total=2**31)


# ---------------- full_clean ----------------

def test_full_clean_accepts_valid_item(rice):
    order = make_order(subtotal=59900)
    OrderItem(
        order=order, product=rice, sku="RICE-5KG", product_name="Basmati Rice",
        product_qty="5kg", unit_price=59900, quantity=1, line_total=59900,
    ).full_clean()


def test_full_clean_rejects_zero_quantity(rice):
    item = OrderItem(
        order=make_order(), product=rice, product_name="x",
        unit_price=100, quantity=0, line_total=0,
    )
    with pytest.raises(ValidationError):
        item.full_clean()


def test_full_clean_rejects_mismatched_line_total(rice):
    item = OrderItem(
        order=make_order(), product=rice, product_name="x",
        unit_price=100, quantity=2, line_total=150,
    )
    with pytest.raises(ValidationError):
        item.full_clean()


def test_full_clean_rejects_duplicate_order_product(rice):
    order = make_order(subtotal=59900)
    add_item(order, rice, 1)
    dup = OrderItem(
        order=order, product=rice, product_name="x",
        unit_price=100, quantity=1, line_total=100,
    )
    with pytest.raises(ValidationError):
        dup.full_clean()


def test_full_clean_rejects_overlong_fields(rice):
    item = OrderItem(
        order=make_order(), product=rice, product_name="N" * 201, sku="S" * 101,
        product_qty="Q" * 65, unit_price=100, quantity=1, line_total=100,
    )
    with pytest.raises(ValidationError) as exc:
        item.full_clean()
    assert {"product_name", "sku", "product_qty"} <= set(exc.value.message_dict)


# ---------------- delete behaviour ----------------

def test_deleting_order_cascades_to_items_but_keeps_products(rice, dal):
    order = make_order(subtotal=59900 + 16500)
    add_item(order, rice, 1)
    add_item(order, dal, 1)

    order.delete()

    assert OrderItem.objects.count() == 0
    assert Product.objects.filter(pk__in=[rice.pk, dal.pk]).count() == 2


def test_product_in_an_order_cannot_be_deleted(rice):
    add_item(make_order(subtotal=59900), rice, 1)
    with pytest.raises(ProtectedError):
        rice.delete()
    assert Product.objects.filter(pk=rice.pk).exists()


def test_deleting_an_item_keeps_order_and_product(rice):
    order = make_order(subtotal=59900)
    item = add_item(order, rice, 1)
    item.delete()
    assert Order.objects.filter(pk=order.pk).exists()
    assert Product.objects.filter(pk=rice.pk).exists()


# ---------------- schema ----------------

def test_product_index_exists():
    indexed = {
        tuple(idx.fields) for idx in OrderItem._meta.indexes
    }
    assert ("product",) in indexed


# ---------------- competing operations (real Postgres, real threads) ----------------

@pytest.mark.django_db(transaction=True)
def test_concurrent_inserts_of_same_order_product_only_one_survives():
    order = make_order(subtotal=100)
    product = Product.objects.create(name="Race", sku="RACE", unit_price=100, inventory=10)

    workers = 8
    barrier = threading.Barrier(workers)
    results = []

    def insert():
        try:
            barrier.wait()
            with transaction.atomic():
                OrderItem.objects.create(
                    order=order, product=product, product_name="Race",
                    unit_price=100, quantity=1, line_total=100,
                )
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
    assert OrderItem.objects.filter(order=order, product=product).count() == 1