"""Cart business rules. Every mutation locks the cart row first, so edits to one cart
(and, in the checkout module, checkout of that cart) are serialised."""
import uuid

from django.db import transaction

from store.api_errors import ApiError
from store.models import Cart, CartItem, Product

MAX_QUANTITY = 100_000


def parse_uuid_or_404(raw, code, message):
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        raise ApiError(404, code, message)


def create_cart():
    return Cart.objects.create()


def get_cart(cart_id):
    try:
        return Cart.objects.get(pk=cart_id)
    except Cart.DoesNotExist:
        raise ApiError(404, "CART_NOT_FOUND", "cart not found")


def _lock_open_cart(cart_id):
    """Must be called inside transaction.atomic()."""
    try:
        cart = Cart.objects.select_for_update().get(pk=cart_id)
    except Cart.DoesNotExist:
        raise ApiError(404, "CART_NOT_FOUND", "cart not found")
    if cart.status != Cart.Status.OPEN:
        raise ApiError(409, "CART_ALREADY_CHECKED_OUT", "cart has already been checked out")
    return cart


def _check_stock(product, wanted):
    # Advisory only: stock can still change before checkout, which re-checks under lock.
    if wanted > product.inventory:
        raise ApiError(409, "INSUFFICIENT_STOCK", "not enough inventory",
                       {"product_id": str(product.id), "requested": wanted,
                        "available": product.inventory})


def add_item(cart_id, product_id, quantity):
    with transaction.atomic():
        cart = _lock_open_cart(cart_id)
        product = Product.objects.filter(pk=product_id).first()
        if product is None:
            raise ApiError(404, "PRODUCT_NOT_FOUND", "product not found")
        item = CartItem.objects.filter(cart=cart, product=product).first()
        new_qty = quantity + (item.quantity if item else 0)  # adding an existing product merges
        if new_qty > MAX_QUANTITY:
            raise ApiError(422, "VALIDATION_ERROR", "request validation failed",
                           {"quantity": [f"Resulting quantity may not exceed {MAX_QUANTITY}."]})
        _check_stock(product, new_qty)
        if item:
            item.quantity = new_qty
            item.save(update_fields=["quantity"])
        else:
            CartItem.objects.create(cart=cart, product=product, quantity=new_qty)
    return cart


def set_item_quantity(cart_id, product_id, quantity):
    with transaction.atomic():
        cart = _lock_open_cart(cart_id)
        item = (CartItem.objects.select_related("product")
                .filter(cart=cart, product_id=product_id).first())
        if item is None:
            raise ApiError(404, "ITEM_NOT_IN_CART", "product is not in this cart")
        _check_stock(item.product, quantity)
        item.quantity = quantity
        item.save(update_fields=["quantity"])
    return cart


def remove_item(cart_id, product_id):
    with transaction.atomic():
        cart = _lock_open_cart(cart_id)
        deleted, _ = CartItem.objects.filter(cart=cart, product_id=product_id).delete()
        if not deleted:
            raise ApiError(404, "ITEM_NOT_IN_CART", "product is not in this cart")
    return cart