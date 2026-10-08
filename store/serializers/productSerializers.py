from django.core.validators import RegexValidator
from rest_framework import serializers

from store.common import RejectUnknownFieldsMixin, StrictCharField, StrictIntegerField

MAX_PRICE = 100_000_000     
MAX_INVENTORY = 1_000_000

sku_validator = RegexValidator(r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
                               "sku may contain letters, digits, '.', '_' and '-' only.")


# ----- input
class _ProductFields(RejectUnknownFieldsMixin, serializers.Serializer):
    name = StrictCharField(max_length=200)
    pack_size = StrictCharField(max_length=64, required=False, allow_blank=True)
    unit_price = StrictIntegerField(min_value=1, max_value=MAX_PRICE)       # minor units
    inventory = StrictIntegerField(min_value=0, max_value=MAX_INVENTORY)


class ProductCreateSerializer(_ProductFields):
    sku = StrictCharField(max_length=100, validators=[sku_validator])


class ProductUpdateSerializer(_ProductFields):
    """Use with partial=True. sku is immutable (not a field => 'Unknown field')."""

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Provide at least one field to update.")
        return attrs


class InventoryAdjustSerializer(RejectUnknownFieldsMixin, serializers.Serializer):
    delta = StrictIntegerField(min_value=-MAX_INVENTORY, max_value=MAX_INVENTORY)

    def validate_delta(self, value):
        if value == 0:
            raise serializers.ValidationError("delta must be non-zero.")
        return value


class ProductQuerySerializer(serializers.Serializer):
    in_stock = serializers.BooleanField(required=False)
    search = serializers.CharField(required=False, max_length=100)


# ----- output
class ProductSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    sku = serializers.CharField()
    name = serializers.CharField()
    pack_size = serializers.CharField(source="product_qty")
    unit_price = serializers.IntegerField()
    inventory = serializers.IntegerField()
    in_stock = serializers.SerializerMethodField()
    updated_at = serializers.DateTimeField()

    def get_in_stock(self, product):
        return product.inventory > 0