from rest_framework import serializers

from store.common import RejectUnknownFieldsMixin, StrictCharField


# ----- input
class CheckoutSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    coupon_code = StrictCharField(max_length=32, required=False, allow_null=True)


# ----- output (money = integer minor units)
class OrderItemSerializer(serializers.Serializer):
    product_id = serializers.UUIDField()
    sku = serializers.CharField()
    name = serializers.CharField(source="product_name")
    pack_size = serializers.CharField(source="product_qty")
    unit_price = serializers.IntegerField()
    quantity = serializers.IntegerField()
    line_total = serializers.IntegerField()


class OrderSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    cart_id = serializers.UUIDField()
    items = serializers.SerializerMethodField()
    subtotal = serializers.IntegerField()
    coupon_code = serializers.SerializerMethodField()
    discount_percent = serializers.IntegerField()
    discount_amount = serializers.IntegerField()
    total = serializers.IntegerField()
    payment_reference = serializers.CharField()
    created_at = serializers.DateTimeField()

    def get_items(self, order):
        lines = order.items.order_by("product_name", "product_id")
        return OrderItemSerializer(lines, many=True).data

    def get_coupon_code(self, order):
        return order.coupon_code or None