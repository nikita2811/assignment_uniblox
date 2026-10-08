# product/management/commands/seed.py
from django.core.management.base import BaseCommand
from django.db import transaction

from store.models import Product

PRODUCTS = [
    # (sku, name, product_qty (pack size), unit_price, inventory)
    ("RICE-BASMATI-5KG", "Basmati Rice",            "5kg",   599, 100),
    ("DAL-TOOR-1KG",     "Toor Dal",                "1kg",   165, 200),
    ("BUTTER-AMUL-500G", "Amul Butter",             "500g",  295,  50),
    ("TEA-TATA-GOLD-500G", "Tata Tea Gold",         "500g",  270,  75),
    ("OIL-OLIVE-1L",     "Cold Pressed Olive Oil",  "1L",    899,  20),
    ("ATTA-ASHIRVAAD-10KG", "Ashirvaad Atta",       "10kg",  485,  80),
    ("SUGAR-1KG",        "Sugar",                   "1kg",    48, 300),
    ("MILK-AMUL-1L",     "Amul Toned Milk",         "1L",     66,  60),
    ("BISCUIT-PARLE-G",  "Parle-G Biscuits",        "800g",   95, 150),
    # Limited stock: use this one for oversell / contention tests and demos
    ("GIFTBOX-LTD",      "Limited Edition Gift Box", "",    1499,   3),
]


class Command(BaseCommand):
    help = "Seed demo products (idempotent, keyed by SKU)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-stock",
            action="store_true",
            help="Reset name, pack size, price and inventory of existing seed products.",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        created = reset = 0
        for sku, name, pack, price, qty in PRODUCTS:
            product, was_created = Product.objects.get_or_create(
                sku=sku,
                defaults={
                    "name": name,
                    "product_qty": pack,
                    "unit_price": price,
                    "inventory": qty,
                },
            )
            if was_created:
                created += 1
            elif opts["reset_stock"]:
                product.name = name
                product.product_qty = pack
                product.unit_price = price
                product.inventory = qty
                product.save()
                reset += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seed complete: {created} created, {reset} reset, "
                f"{Product.objects.count()} products total."
            )
        )