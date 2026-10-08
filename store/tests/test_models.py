import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F

from store.models import Product


@pytest.fixture
def product(db):
    return Product.objects.create(name="Widget", unit_price=1999, inventory=5)


@pytest.mark.django_db
def test_price_is_stored_as_integer_cents(product):
    product.refresh_from_db()
    assert product.unit_price == 1999
    assert isinstance(product.unit_price, int)


@pytest.mark.django_db
def test_inventory_can_be_zero():
    p = Product.objects.create(name="Sold out", unit_price=100, inventory=0)
    p.refresh_from_db()
    assert p.inventory == 0


@pytest.mark.django_db
@pytest.mark.parametrize("bad_price", [0, -1])
def test_db_rejects_non_positive_price(bad_price):
    with pytest.raises(IntegrityError), transaction.atomic():
        Product.objects.create(name="Bad", unit_price=bad_price, inventory=1)


@pytest.mark.django_db
def test_db_rejects_negative_inventory_on_create():
    with pytest.raises(IntegrityError), transaction.atomic():
        Product.objects.create(name="Bad", unit_price=100, inventory=-1)