from rest_framework import serializers

from store.models import Coupon


class CouponQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[c.value for c in Coupon.Status], required=False)


class CouponSerializer(serializers.Serializer):
    code = serializers.CharField()
    milestone = serializers.IntegerField()
    discount_percent = serializers.IntegerField()
    status = serializers.CharField()
    created_at = serializers.DateTimeField()
    redeemed_at = serializers.DateTimeField()
    order_id = serializers.SerializerMethodField()

    def get_order_id(self, coupon):
        order = getattr(coupon, "order", None)  # reverse one-to-one; None until redeemed
        return str(order.id) if order else None