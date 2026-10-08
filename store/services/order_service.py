"""Checkout. Everything happens in ONE transaction, so a failure at any step (stock, coupon,
payment, DB) rolls back inventory, coupon and order together.

Lock order is always: cart -> products (sorted by id) -> coupon. A fixed order is what prevents
deadlocks between concurrent checkouts that share products."""
import uuid

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from store.api_errors import ApiError
from store.common import parse_uuid_or_404
from store.models import Cart, CartItem, Coupon, Order, OrderItem, Product

from store import payment


def compute_discount(subtotal, percent):
    """Integer floor of subtotal * percent / 100: deterministic, never exceeds subtotal."""
    return subtotal * percent // 100


def get_order(order_id):
    oid = parse_uuid_or_404(order_id, "ORDER_NOT_FOUND", "order not found")
    try:
        return Order.objects.get(pk=oid)
    except Order.DoesNotExist:
        raise ApiError(404, "ORDER_NOT_FOUND", "order not found")


def _replay(order, cart_id, coupon_code):
    """Same key must mean same request; a different request under one key is a client bug."""
    if order.cart_id != cart_id or order.coupon_code != coupon_code:
        raise ApiError(422, "IDEMPOTENCY_KEY_REUSED",
                       "Idempotency-Key was already used for a different request")
    return order


def checkout(cart_id, coupon_code=None, idempotency_key=None):
    """Returns (order, replayed). Failed attempts store nothing, so their key can be retried."""
    code = (coupon_code or "").strip().upper()
    try:
        return _checkout(cart_id, code, idempotency_key)
    except IntegrityError:
        # Lost a race on the unique idempotency key held by another cart's order.
        existing = Order.objects.filter(idempotency_key=idempotency_key).first() if idempotency_key else None
        if existing is None:
            raise
        return _replay(existing, cart_id, code), True


def _checkout(cart_id, coupon_code, key):
    with transaction.atomic():
        if key:
            existing = Order.objects.filter(idempotency_key=key).first()
            if existing:
                return _replay(existing, cart_id, coupon_code), True

        try:
            cart = Cart.objects.select_for_update().get(pk=cart_id)
        except Cart.DoesNotExist:
            raise ApiError(404, "CART_NOT_FOUND", "cart not found")

        if cart.status == Cart.Status.CHECKED_OUT:
            order = Order.objects.filter(cart=cart).first()
            if key and order and order.idempotency_key == key:  # concurrent retry, same key
                return _replay(order, cart.id, coupon_code), True
            raise ApiError(409, "CART_ALREADY_CHECKED_OUT", "cart has already been checked out",
                           {"order_id": str(order.id) if order else None})

        items = list(CartItem.objects.filter(cart=cart).order_by("product_id"))
        if not items:
            raise ApiError(422, "EMPTY_CART", "cart has no items")

        products = {p.id: p for p in Product.objects.select_for_update()
                    .filter(id__in=[i.product_id for i in items]).order_by("id")}

        shortages = [{"product_id": str(i.product_id), "requested": i.quantity,
                      "available": products[i.product_id].inventory}
                     for i in items if i.quantity > products[i.product_id].inventory]
        if shortages:
            raise ApiError(409, "INSUFFICIENT_STOCK", "some items are no longer available",
                           {"items": shortages})

        # Price policy: charge the CURRENT price and snapshot it on the order.
        subtotal = sum(products[i.product_id].unit_price * i.quantity for i in items)

        coupon, percent, discount = None, None, 0
        if coupon_code:
            coupon = Coupon.objects.select_for_update().filter(code=coupon_code).first()
            if coupon is None:
                raise ApiError(422, "COUPON_NOT_FOUND", "coupon does not exist")
            if coupon.status != Coupon.Status.AVAILABLE:
                raise ApiError(409, "COUPON_ALREADY_REDEEMED", "coupon has already been redeemed")
            percent = coupon.discount_percent
            discount = compute_discount(subtotal, percent)
            coupon.status = Coupon.Status.REDEEMED
            coupon.redeemed_at = timezone.now()
            coupon.save(update_fields=["status", "redeemed_at"])

        total = subtotal - discount
        try:
            reference = payment.gateway.charge(total, idempotency_key=key or str(cart.id))
        except payment.PaymentError as exc:
            raise ApiError(402, "PAYMENT_FAILED", "payment was not accepted", {"reason": str(exc)})

        order = Order.objects.create(
            cart=cart, idempotency_key=key, coupon=coupon,
            coupon_code=coupon_code if coupon else "", discount_percent=percent,
            subtotal=subtotal, discount_amount=discount, total=total,
            payment_reference=reference)
        OrderItem.objects.bulk_create([
            OrderItem(order=order, product=products[i.product_id],
                      sku=products[i.product_id].sku or "",
                      product_name=products[i.product_id].name,
                      product_qty=products[i.product_id].product_qty,
                      unit_price=products[i.product_id].unit_price, quantity=i.quantity,
                      line_total=products[i.product_id].unit_price * i.quantity)
            for i in items])
        for i in items:
            Product.objects.filter(pk=i.product_id).update(inventory=F("inventory") - i.quantity)
        cart.status = Cart.Status.CHECKED_OUT
        cart.save(update_fields=["status"])
        return order, False