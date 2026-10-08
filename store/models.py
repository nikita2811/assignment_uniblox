from django.db import models

# Create your models here.
from django.db.models import Q
import uuid
from django.utils.text import slugify
import secrets


class Product(models.Model):
    id= models.UUIDField(default=uuid.uuid4,primary_key=True,editable=False)
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=100, unique=True, null=True, blank=True) # added for idempotency
    product_qty = models.CharField(max_length=64,blank=True) # pack size, e.g. "5kg", "500g"
    unit_price = models.PositiveIntegerField()
    inventory = models.PositiveIntegerField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(unit_price__gt=0), name="product_price_positive"),
            models.CheckConstraint(condition=Q(inventory__gte=0), name="product_inventory_non_negative"),
        ]

    def _generate_sku(self):
        base = f"{slugify(self.name).upper()[:45]}-{self.product_qty}"
        sku, n = base, 1
        while Product.objects.filter(sku=sku).exclude(pk=self.pk).exists():
            n += 1
            sku = f"{base}-{n}"
        return sku

    def save(self, *args, **kwargs):
        if not self.sku:
            self.sku = self._generate_sku()
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"sku"}
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Cart(models.Model):
    class Status(models.TextChoices):
        OPEN = "open"
        CHECKED_OUT = "checked_out"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    created_at = models.DateTimeField(auto_now_add=True)


class CartItem(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.IntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cart", "product"], name="uniq_cart_product"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="cartitem_qty_gt_0"
            ),
        ]



def generate_coupon_code():
    return f"SAVE-{secrets.token_hex(5).upper()}"


class Coupon(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "available"
        REDEEMED = "redeemed"

    code = models.CharField(max_length=32, unique=True, default=generate_coupon_code)
    milestone = models.PositiveIntegerField(unique=True)        
    discount_percent = models.PositiveSmallIntegerField()      
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.AVAILABLE
    )
    created_at = models.DateTimeField(auto_now_add=True)
    redeemed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["milestone"]
        constraints = [
            models.CheckConstraint(
                condition=Q(discount_percent__gte=1) & Q(discount_percent__lte=100),
                name="coupon_percent_1_100",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="redeemed", redeemed_at__isnull=False)
                    | Q(status="available", redeemed_at__isnull=True)
                ),
                name="coupon_status_redeemed_at_consistent",
            ),
        ]

    def __str__(self):
        return f"{self.code} ({self.discount_percent}% off, {self.status})"