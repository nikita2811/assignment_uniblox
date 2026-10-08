from rest_framework import serializers

from store.services.cart_services import MAX_QUANTITY


class StrictIntegerField(serializers.IntegerField):
    """DRF's IntegerField happily coerces "2" or 2.0; a quantity must be a real JSON integer."""

    def to_internal_value(self, data):
        if isinstance(data, bool) or not isinstance(data, int):
            self.fail("invalid")
        return super().to_internal_value(data)


# ----- input
class AddCartItemSerializer(serializers.Serializer):
    product_id = serializers.UUIDField()
    quantity = StrictIntegerField(min_value=1, max_value=MAX_QUANTITY)


class UpdateCartItemSerializer(serializers.Serializer):
    quantity = StrictIntegerField(min_value=1, max_value=MAX_QUANTITY)


# ----- output (money is integer minor units, e.g. paise)
class CartItemSerializer(serializers.Serializer):
    product_id = serializers.UUIDField(source="product.id")
    sku = serializers.CharField(source="product.sku")
    name = serializers.CharField(source="product.name")
    pack_size = serializers.CharField(source="product.product_qty")
    unit_price = serializers.IntegerField(source="product.unit_price")
    quantity = serializers.IntegerField()
    line_total = serializers.SerializerMethodField()
    available_inventory = serializers.IntegerField(source="product.inventory")

    def get_line_total(self, item):
        return item.product.unit_price * item.quantity


class CartSerializer(serializers.Serializer):
    """Cart prices are LIVE (current product price). The order snapshots them at checkout."""

    def to_representation(self, cart):
        items = list(cart.items.select_related("product").order_by("product__name", "product_id"))
        subtotal = sum(i.product.unit_price * i.quantity for i in items)
        warnings = [{"code": "INSUFFICIENT_STOCK", "product_id": str(i.product_id),
                     "requested": i.quantity, "available": i.product.inventory}
                    for i in items if i.quantity > i.product.inventory]
        order = getattr(cart, "order", None)  # reverse one-to-one; None until checked out
        return {
            "id": str(cart.id),
            "status": cart.status,
            "items": CartItemSerializer(items, many=True).data,
            "subtotal": subtotal,
            "warnings": warnings,
            "order_id": str(order.id) if order else None,
            "checkout_ready": cart.status == "open" and bool(items) and not warnings,
        }