from django.db import models

# Create your models here.
from django.db.models import Q
import uuid
from django.utils.text import slugify


class Product(models.Model):
    id= models.UUIDField(default=uuid.uuid4,primary_key=True,editable=False)
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=100, unique=True, null=True, blank=True)
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
