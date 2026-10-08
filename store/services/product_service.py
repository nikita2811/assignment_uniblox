"""Product rules. Inventory/price writes lock the product row, which is the same lock the
checkout module takes, so admin changes and checkouts are serialised per product."""
from django.db import IntegrityError, transaction
from django.db.models import Q

from store.api_errors import ApiError
from store.common import parse_uuid_or_404
from store.models import Product

from store.serializers.productSerializers import MAX_INVENTORY

FIELD_MAP = {"name": "name", "pack_size": "product_qty",
             "unit_price": "unit_price", "inventory": "inventory"}


def _id(raw):
    return parse_uuid_or_404(raw, "PRODUCT_NOT_FOUND", "product not found")


def list_products(in_stock=None, search=None):
    qs = Product.objects.all()
    if in_stock is True:
        qs = qs.filter(inventory__gt=0)
    elif in_stock is False:
        qs = qs.filter(inventory=0)
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(sku__icontains=search))
    return qs.order_by("name", "id")


def get_product(product_id):
    try:
        return Product.objects.get(pk=_id(product_id))
    except Product.DoesNotExist:
        raise ApiError(404, "PRODUCT_NOT_FOUND", "product not found")


def _lock(product_id):
    try:
        return Product.objects.select_for_update().get(pk=_id(product_id))
    except Product.DoesNotExist:
        raise ApiError(404, "PRODUCT_NOT_FOUND", "product not found")


def create_product(data):
    try:
        with transaction.atomic():
            return Product.objects.create(
                name=data["name"], sku=data["sku"], product_qty=data.get("pack_size", ""),
                unit_price=data["unit_price"], inventory=data["inventory"])
    except IntegrityError:  # the only unique column we write is sku; DB is the source of truth
        raise ApiError(409, "SKU_ALREADY_EXISTS", "a product with this sku already exists",
                       {"sku": data["sku"]})


def update_product(product_id, data):
    with transaction.atomic():
        product = _lock(product_id)
        for key, value in data.items():
            setattr(product, FIELD_MAP[key], value)
        # auto_now fields are only written when named in update_fields
        product.save(update_fields=[FIELD_MAP[k] for k in data] + ["updated_at"])
    return product


def adjust_inventory(product_id, delta):
    """Relative change (restock / write-off). Unlike PATCH inventory (absolute overwrite),
    concurrent adjustments never lose each other's updates."""
    with transaction.atomic():
        product = _lock(product_id)
        new_value = product.inventory + delta
        if new_value < 0:
            raise ApiError(409, "INVENTORY_CANNOT_BE_NEGATIVE", "adjustment would make inventory negative",
                           {"current": product.inventory, "delta": delta})
        if new_value > MAX_INVENTORY:
            raise ApiError(422, "VALIDATION_ERROR", "request validation failed",
                           {"delta": [f"Resulting inventory may not exceed {MAX_INVENTORY}."]})
        product.inventory = new_value
        product.save(update_fields=["inventory", "updated_at"])
    return product