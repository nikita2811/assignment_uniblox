import threading
import time
import uuid
from unittest import skipUnless

from django.db import IntegrityError, connection, connections, transaction
from django.db.models import F
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient, APITestCase

from store.models import Product
from store.serializers.productSerializers import MAX_INVENTORY, MAX_PRICE


# ------------------------------------------------------------------- helpers
def make_product(name="Rice", price=10000, inventory=10, sku=None, pack=""):
    return Product.objects.create(name=name, sku=sku or f"T-{uuid.uuid4().hex[:10]}",
                                  product_qty=pack, unit_price=price, inventory=inventory)


def valid_payload(**overrides):
    payload = {"name": "Basmati Rice", "sku": f"RICE-{uuid.uuid4().hex[:6]}", "pack_size": "5kg",
               "unit_price": 64900, "inventory": 50}
    payload.update(overrides)
    return payload


def assert_error(test, resp, status_code, code):
    test.assertEqual(resp.status_code, status_code, resp.content)
    body = resp.json()["error"]
    test.assertEqual(body["code"], code)
    test.assertIn("message", body)
    test.assertIn("details", body)
    return body


def run_parallel(fn, n):
    barrier, results = threading.Barrier(n), [None] * n

    def worker(i):
        try:
            barrier.wait()
            results[i] = fn(i)
        except Exception as exc:
            results[i] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    for r in results:
        assert not isinstance(r, Exception), r
    return results


# --------------------------------------------------------- GET /products/ (public)
class ProductListTests(APITestCase):
    def test_empty_catalog(self):
        resp = self.client.get("/api/products/")
        self.assertEqual((resp.status_code, resp.json()), (200, {"count": 0, "results": []}))

    def test_lists_products_sorted_with_all_fields(self):
        make_product("Sugar", 4800, 200, sku="SUGAR-1KG", pack="1kg")
        make_product("Atta", 41500, 40, sku="ATTA-10KG", pack="10kg")
        data = self.client.get("/api/products/").json()
        self.assertEqual(data["count"], 2)
        self.assertEqual([p["name"] for p in data["results"]], ["Atta", "Sugar"])
        atta = data["results"][0]
        self.assertEqual({k: atta[k] for k in ["sku", "pack_size", "unit_price", "inventory", "in_stock"]},
                         {"sku": "ATTA-10KG", "pack_size": "10kg", "unit_price": 41500,
                          "inventory": 40, "in_stock": True})
        uuid.UUID(atta["id"])
        self.assertIn("updated_at", atta)

    def test_limited_and_sold_out_products(self):
        make_product("Saffron", inventory=3)
        make_product("Gone", inventory=0)
        by_name = {p["name"]: p for p in self.client.get("/api/products/").json()["results"]}
        self.assertEqual((by_name["Saffron"]["inventory"], by_name["Saffron"]["in_stock"]), (3, True))
        self.assertEqual((by_name["Gone"]["inventory"], by_name["Gone"]["in_stock"]), (0, False))

    def test_in_stock_filter(self):
        make_product("Have", inventory=5)
        make_product("Gone", inventory=0)
        names = lambda q: [p["name"] for p in self.client.get(f"/api/products/{q}").json()["results"]]
        self.assertEqual(names("?in_stock=true"), ["Have"])
        self.assertEqual(names("?in_stock=false"), ["Gone"])
        self.assertEqual(names(""), ["Gone", "Have"])

    def test_search_matches_name_or_sku_case_insensitively(self):
        make_product("Basmati Rice", sku="RICE-5KG")
        make_product("Sunflower Oil", sku="OIL-1L")
        names = lambda q: [p["name"] for p in self.client.get("/api/products/", {"search": q}).json()["results"]]
        self.assertEqual(names("rice"), ["Basmati Rice"])
        self.assertEqual(names("oil-1"), ["Sunflower Oil"])
        self.assertEqual(names("nothing"), [])

    def test_filters_combine(self):
        make_product("Rice Big", inventory=0)
        make_product("Rice Small", inventory=4)
        resp = self.client.get("/api/products/", {"search": "rice", "in_stock": "true"}).json()
        self.assertEqual([p["name"] for p in resp["results"]], ["Rice Small"])

    def test_invalid_filter_value_is_validation_error(self):
        body = assert_error(self, self.client.get("/api/products/?in_stock=maybe"), 422, "VALIDATION_ERROR")
        self.assertIn("in_stock", body["details"])

    def test_listing_is_read_only(self):
        p = make_product()
        before = Product.objects.values().get(pk=p.pk)
        for _ in range(3):
            self.client.get("/api/products/")
        self.assertEqual(Product.objects.values().get(pk=p.pk), before)


class ProductDetailTests(APITestCase):
    def test_retrieve(self):
        p = make_product("Rice", 10000, 7, sku="RICE-5KG", pack="5kg")
        data = self.client.get(f"/api/products/{p.id}/").json()
        self.assertEqual((data["id"], data["name"], data["unit_price"], data["inventory"]),
                         (str(p.id), "Rice", 10000, 7))

    def test_unknown_or_malformed_id_is_json_404(self):
        for pid in [uuid.uuid4(), "garbage"]:
            assert_error(self, self.client.get(f"/api/products/{pid}/"), 404, "PRODUCT_NOT_FOUND")

    def test_write_methods_not_allowed_on_public_route(self):
        p = make_product()
        for method in (self.client.post, self.client.patch, self.client.delete):
            assert_error(self, method(f"/api/products/{p.id}/", {}, format="json"), 405, "METHOD_NOT_ALLOWED")


# ------------------------------------------------- POST /admin/products/ (create)
class AdminCreateTests(APITestCase):
    def test_create_returns_201_and_persists_exact_values(self):
        payload = valid_payload(sku="SAFFRON-1G", name="Saffron", pack_size="1g",
                                unit_price=39900, inventory=3)
        resp = self.client.post("/api/admin/products/", payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        p = Product.objects.get(pk=resp.json()["id"])
        self.assertEqual((p.sku, p.name, p.product_qty, p.unit_price, p.inventory),
                         ("SAFFRON-1G", "Saffron", "1g", 39900, 3))
        self.assertEqual(resp.json()["in_stock"], True)
        self.assertEqual(self.client.get(f"/api/products/{p.id}/").json()["id"], str(p.id))

    def test_pack_size_is_optional_and_zero_inventory_allowed(self):
        payload = valid_payload(inventory=0)
        del payload["pack_size"]
        resp = self.client.post("/api/admin/products/", payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual((resp.json()["pack_size"], resp.json()["in_stock"]), ("", False))

    def test_missing_required_fields_are_all_reported(self):
        body = assert_error(self, self.client.post("/api/admin/products/", {}, format="json"),
                            422, "VALIDATION_ERROR")
        self.assertEqual(set(body["details"]), {"name", "sku", "unit_price", "inventory"})

    def test_invalid_price_values(self):
        for bad in [0, -5, "100", 10.5, True, None, MAX_PRICE + 1]:
            body = assert_error(self, self.client.post("/api/admin/products/",
                                valid_payload(unit_price=bad), format="json"), 422, "VALIDATION_ERROR")
            self.assertIn("unit_price", body["details"], bad)
        self.assertEqual(Product.objects.count(), 0)

    def test_invalid_inventory_values(self):
        for bad in [-1, "5", 1.5, True, None, MAX_INVENTORY + 1]:
            body = assert_error(self, self.client.post("/api/admin/products/",
                                valid_payload(inventory=bad), format="json"), 422, "VALIDATION_ERROR")
            self.assertIn("inventory", body["details"], bad)
        self.assertEqual(Product.objects.count(), 0)

    def test_invalid_text_fields(self):
        cases = {"name": ["", "   ", 123, "x" * 201, None],
                 "sku": ["", "has space", "bad/slash", 5, "x" * 101],
                 "pack_size": [5, "x" * 65]}
        for field, values in cases.items():
            for bad in values:
                body = assert_error(self, self.client.post("/api/admin/products/",
                                    valid_payload(**{field: bad}), format="json"), 422, "VALIDATION_ERROR")
                self.assertIn(field, body["details"], (field, bad))
        self.assertEqual(Product.objects.count(), 0)

    def test_unknown_field_is_rejected_not_ignored(self):
        body = assert_error(self, self.client.post("/api/admin/products/",
                            valid_payload(price=5), format="json"), 422, "VALIDATION_ERROR")
        self.assertIn("price", body["details"])

    def test_duplicate_sku_is_409(self):
        make_product(sku="RICE-5KG")
        body = assert_error(self, self.client.post("/api/admin/products/",
                            valid_payload(sku="RICE-5KG"), format="json"), 409, "SKU_ALREADY_EXISTS")
        self.assertEqual(body["details"]["sku"], "RICE-5KG")
        self.assertEqual(Product.objects.count(), 1)

    def test_malformed_json(self):
        resp = self.client.post("/api/admin/products/", data="{bad", content_type="application/json")
        assert_error(self, resp, 400, "PARSE_ERROR")


# ------------------------------------------------ PATCH /admin/products/{id}/ (update)
class AdminUpdateTests(APITestCase):
    def setUp(self):
        self.product = make_product("Rice", 10000, 10, sku="RICE-5KG", pack="5kg")
        self.url = f"/api/admin/products/{self.product.id}/"

    def row(self):
        return Product.objects.values("name", "sku", "product_qty", "unit_price", "inventory").get(pk=self.product.pk)

    def test_patch_changes_only_the_fields_sent(self):
        before = self.row()
        resp = self.client.patch(self.url, {"unit_price": 12000}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.row(), {**before, "unit_price": 12000})

    def test_patch_multiple_fields_and_updated_at_moves(self):
        old = Product.objects.get(pk=self.product.pk).updated_at
        time.sleep(0.01)
        resp = self.client.patch(self.url, {"name": "Premium Rice", "pack_size": "10kg",
                                            "inventory": 0}, format="json")
        self.assertEqual((resp.json()["name"], resp.json()["pack_size"], resp.json()["in_stock"]),
                         ("Premium Rice", "10kg", False))
        self.assertGreater(Product.objects.get(pk=self.product.pk).updated_at, old)

    def test_sku_is_immutable(self):
        body = assert_error(self, self.client.patch(self.url, {"sku": "NEW"}, format="json"),
                            422, "VALIDATION_ERROR")
        self.assertIn("sku", body["details"])
        self.assertEqual(self.row()["sku"], "RICE-5KG")

    def test_empty_patch_is_rejected(self):
        assert_error(self, self.client.patch(self.url, {}, format="json"), 422, "VALIDATION_ERROR")

    def test_invalid_values_leave_row_untouched(self):
        before = self.row()
        for payload in [{"unit_price": 0}, {"unit_price": "5"}, {"inventory": -1}, {"inventory": 2.5},
                        {"name": "  "}, {"unit_price": 100, "inventory": -1}]:
            assert_error(self, self.client.patch(self.url, payload, format="json"), 422, "VALIDATION_ERROR")
        self.assertEqual(self.row(), before)

    def test_unknown_product(self):
        for pid in [uuid.uuid4(), "garbage"]:
            assert_error(self, self.client.patch(f"/api/admin/products/{pid}/", {"unit_price": 5},
                                                 format="json"), 404, "PRODUCT_NOT_FOUND")

    def test_price_change_shows_up_in_existing_carts(self):  # policy: carts use live prices
        cart_id = self.client.post("/api/carts/", format="json").json()["id"]
        self.client.post(f"/api/carts/{cart_id}/items/",
                         {"product_id": str(self.product.id), "quantity": 2}, format="json")
        self.client.patch(self.url, {"unit_price": 15000}, format="json")
        cart = self.client.get(f"/api/carts/{cart_id}/").json()
        self.assertEqual((cart["items"][0]["unit_price"], cart["subtotal"]), (15000, 30000))


# ------------------------------------ POST /admin/products/{id}/adjust-inventory/
class AdminAdjustInventoryTests(APITestCase):
    def setUp(self):
        self.product = make_product(inventory=5)
        self.url = f"/api/admin/products/{self.product.id}/adjust-inventory/"

    def inventory(self):
        return Product.objects.get(pk=self.product.pk).inventory

    def test_restock_and_writeoff_are_relative(self):
        self.assertEqual(self.client.post(self.url, {"delta": 7}, format="json").json()["inventory"], 12)
        self.assertEqual(self.client.post(self.url, {"delta": -2}, format="json").json()["inventory"], 10)
        self.assertEqual(self.inventory(), 10)

    def test_can_reach_exactly_zero(self):
        resp = self.client.post(self.url, {"delta": -5}, format="json")
        self.assertEqual((resp.status_code, resp.json()["in_stock"]), (200, False))

    def test_cannot_go_negative(self):
        body = assert_error(self, self.client.post(self.url, {"delta": -6}, format="json"),
                            409, "INVENTORY_CANNOT_BE_NEGATIVE")
        self.assertEqual((body["details"]["current"], body["details"]["delta"]), (5, -6))
        self.assertEqual(self.inventory(), 5)

    def test_cannot_exceed_max(self):
        assert_error(self, self.client.post(self.url, {"delta": MAX_INVENTORY}, format="json"),
                     422, "VALIDATION_ERROR")
        self.assertEqual(self.inventory(), 5)

    def test_invalid_delta(self):
        for bad in [0, "3", 1.5, True, None]:
            body = assert_error(self, self.client.post(self.url, {"delta": bad}, format="json"),
                                422, "VALIDATION_ERROR")
            self.assertIn("delta", body["details"], bad)
        assert_error(self, self.client.post(self.url, {}, format="json"), 422, "VALIDATION_ERROR")
        self.assertEqual(self.inventory(), 5)

    def test_unknown_product(self):
        assert_error(self, self.client.post(f"/api/admin/products/{uuid.uuid4()}/adjust-inventory/",
                                            {"delta": 1}, format="json"), 404, "PRODUCT_NOT_FOUND")


# -------------------------------------------------------------- DB-level backstops
class ProductConstraintTests(TestCase):
    def test_price_must_be_positive(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_product(price=0)

    def test_inventory_cannot_go_negative(self):
        p = make_product(inventory=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.filter(pk=p.pk).update(inventory=F("inventory") - 2)

    def test_sku_unique(self):
        make_product(sku="DUP")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_product(sku="DUP")


# ------------------------------------------------------------------- concurrency
@skipUnless(connection.vendor == "postgresql", "needs PostgreSQL row-level locking")
class ProductConcurrencyTests(TransactionTestCase):
    def test_parallel_restocks_do_not_lose_updates(self):
        p = make_product(inventory=0)
        url = f"/api/admin/products/{p.id}/adjust-inventory/"
        results = run_parallel(lambda i: APIClient().post(url, {"delta": 1}, format="json"), 10)
        self.assertEqual([r.status_code for r in results], [200] * 10)
        p.refresh_from_db()
        self.assertEqual(p.inventory, 10)

    def test_parallel_writeoffs_never_go_below_zero(self):
        p = make_product(inventory=3)
        url = f"/api/admin/products/{p.id}/adjust-inventory/"
        results = run_parallel(lambda i: APIClient().post(url, {"delta": -1}, format="json"), 8)
        self.assertEqual(sorted(r.status_code for r in results), [200] * 3 + [409] * 5)
        p.refresh_from_db()
        self.assertEqual(p.inventory, 0)

    def test_parallel_creates_with_same_sku_yield_one_product(self):
        payload = valid_payload(sku="SAME-SKU")
        results = run_parallel(
            lambda i: APIClient().post("/api/admin/products/", payload, format="json"), 6)
        self.assertEqual(sorted(r.status_code for r in results), [201] + [409] * 5,
                         [r.content for r in results])
        self.assertEqual(Product.objects.filter(sku="SAME-SKU").count(), 1)