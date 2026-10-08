"""Cart module tests.  Run: python manage.py test store.cart
Concurrency tests need PostgreSQL (row locks) and are skipped on other databases."""
import threading
import uuid
from unittest import skipUnless

from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient, APITestCase

from store.models import Cart, CartItem, Order, Product

from store.services import MAX_QUANTITY


# ------------------------------------------------------------------- helpers
def make_product(name="Rice", price=10000, inventory=10):
    return Product.objects.create(name=name, sku=f"T-{uuid.uuid4().hex[:10]}",
                                  unit_price=price, inventory=inventory)


def new_cart(client):
    resp = client.post("/api/carts/", format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()["id"]


def add(client, cart_id, product, qty):
    return client.post(f"/api/carts/{cart_id}/items/",
                       {"product_id": str(product.id), "quantity": qty}, format="json")


def item_url(cart_id, product):
    return f"/api/carts/{cart_id}/items/{product.id}/"


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


# ------------------------------------------------------- POST /carts/  GET /carts/{id}/
class CartCreateAndViewTests(APITestCase):
    def test_create_returns_empty_open_cart(self):
        resp = self.client.post("/api/carts/", format="json")
        self.assertEqual(resp.status_code, 201)
        cart = resp.json()
        uuid.UUID(cart["id"])
        self.assertEqual((cart["status"], cart["items"], cart["subtotal"]), ("open", [], 0))
        self.assertEqual((cart["order_id"], cart["checkout_ready"]), (None, False))
        self.assertEqual(Cart.objects.count(), 1)

    def test_view_returns_prices_line_totals_and_subtotal(self):
        rice, oil = make_product("Rice", 10000), make_product("Oil", 2550)
        cart_id = new_cart(self.client)
        add(self.client, cart_id, rice, 2)
        add(self.client, cart_id, oil, 3)
        cart = self.client.get(f"/api/carts/{cart_id}/").json()
        self.assertEqual([i["name"] for i in cart["items"]], ["Oil", "Rice"])  # stable order
        self.assertEqual({i["name"]: (i["unit_price"], i["quantity"], i["line_total"])
                          for i in cart["items"]},
                         {"Rice": (10000, 2, 20000), "Oil": (2550, 3, 7650)})
        self.assertEqual(cart["subtotal"], 27650)
        self.assertEqual((cart["warnings"], cart["checkout_ready"]), ([], True))

    def test_unknown_or_malformed_cart_id_is_json_404(self):
        for cid in [str(uuid.uuid4()), "not-a-uuid"]:
            assert_error(self, self.client.get(f"/api/carts/{cid}/"), 404, "CART_NOT_FOUND")

    def test_view_is_read_only(self):
        p = make_product()
        cart_id = new_cart(self.client)
        add(self.client, cart_id, p, 1)
        before = list(CartItem.objects.values())
        for _ in range(3):
            self.client.get(f"/api/carts/{cart_id}/")
        self.assertEqual(list(CartItem.objects.values()), before)

    def test_unsupported_method_uses_error_contract(self):
        cart_id = new_cart(self.client)
        assert_error(self, self.client.put(f"/api/carts/{cart_id}/", {}, format="json"),
                     405, "METHOD_NOT_ALLOWED")


# ------------------------------------------------------------- POST /carts/{id}/items/
class AddItemTests(APITestCase):
    def test_add_item_creates_line_and_returns_cart(self):
        p = make_product("Rice", 10000, inventory=10)
        cart_id = new_cart(self.client)
        resp = add(self.client, cart_id, p, 2)
        self.assertEqual(resp.status_code, 201)
        cart = resp.json()
        self.assertEqual(cart["subtotal"], 20000)
        line = cart["items"][0]
        self.assertEqual((line["product_id"], line["quantity"], line["available_inventory"]),
                         (str(p.id), 2, 10))

    def test_adding_same_product_again_merges_quantity(self):
        p = make_product()
        cart_id = new_cart(self.client)
        add(self.client, cart_id, p, 2)
        resp = add(self.client, cart_id, p, 3)
        self.assertEqual(resp.json()["items"][0]["quantity"], 5)
        self.assertEqual(CartItem.objects.count(), 1)

    def test_invalid_quantities_are_rejected_and_never_stored(self):
        p = make_product()
        cart_id = new_cart(self.client)
        for bad in [0, -1, "2", 1.5, True, None, [], {}]:
            body = assert_error(self, add(self.client, cart_id, p, bad), 422, "VALIDATION_ERROR")
            self.assertIn("quantity", body["details"], bad)
        resp = self.client.post(f"/api/carts/{cart_id}/items/",
                                {"product_id": str(p.id)}, format="json")  # quantity missing
        self.assertIn("quantity", assert_error(self, resp, 422, "VALIDATION_ERROR")["details"])
        self.assertEqual(CartItem.objects.count(), 0)

    def test_quantity_upper_bound(self):
        p = make_product(inventory=10)
        cart_id = new_cart(self.client)
        assert_error(self, add(self.client, cart_id, p, MAX_QUANTITY + 1), 422, "VALIDATION_ERROR")

    def test_invalid_product_id_is_validation_error(self):
        cart_id = new_cart(self.client)
        for payload in [{"product_id": "nope", "quantity": 1}, {"quantity": 1}]:
            resp = self.client.post(f"/api/carts/{cart_id}/items/", payload, format="json")
            self.assertIn("product_id", assert_error(self, resp, 422, "VALIDATION_ERROR")["details"])

    def test_unknown_product_is_404(self):
        cart_id = new_cart(self.client)
        resp = self.client.post(f"/api/carts/{cart_id}/items/",
                                {"product_id": str(uuid.uuid4()), "quantity": 1}, format="json")
        assert_error(self, resp, 404, "PRODUCT_NOT_FOUND")
        self.assertEqual(CartItem.objects.count(), 0)

    def test_cannot_exceed_inventory_including_merged_quantity(self):
        p = make_product(inventory=3)
        cart_id = new_cart(self.client)
        add(self.client, cart_id, p, 2)
        body = assert_error(self, add(self.client, cart_id, p, 2), 409, "INSUFFICIENT_STOCK")
        self.assertEqual((body["details"]["requested"], body["details"]["available"]), (4, 3))
        self.assertEqual(CartItem.objects.get().quantity, 2)  # unchanged

    def test_unknown_cart_is_404(self):
        p = make_product()
        resp = add(self.client, uuid.uuid4(), p, 1)
        assert_error(self, resp, 404, "CART_NOT_FOUND")

    def test_malformed_json_uses_error_contract(self):
        cart_id = new_cart(self.client)
        resp = self.client.post(f"/api/carts/{cart_id}/items/", data="{bad",
                                content_type="application/json")
        assert_error(self, resp, 400, "PARSE_ERROR")

    def test_non_object_body_is_validation_error(self):
        cart_id = new_cart(self.client)
        resp = self.client.post(f"/api/carts/{cart_id}/items/", [1, 2], format="json")
        assert_error(self, resp, 422, "VALIDATION_ERROR")


# ------------------------------------------- PATCH / DELETE /carts/{id}/items/{product_id}/
class UpdateAndRemoveItemTests(APITestCase):
    def setUp(self):
        self.product = make_product(inventory=5)
        self.cart_id = new_cart(self.client)
        add(self.client, self.cart_id, self.product, 2)
        self.url = item_url(self.cart_id, self.product)

    def test_patch_sets_quantity_absolutely(self):
        resp = self.client.patch(self.url, {"quantity": 4}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["items"][0]["quantity"], 4)  # set, not added
        resp = self.client.patch(self.url, {"quantity": 1}, format="json")
        self.assertEqual(resp.json()["items"][0]["quantity"], 1)

    def test_patch_rejects_invalid_quantities(self):
        for bad in [0, -3, "4", 2.5, None]:
            assert_error(self, self.client.patch(self.url, {"quantity": bad}, format="json"),
                         422, "VALIDATION_ERROR")
        assert_error(self, self.client.patch(self.url, {}, format="json"), 422, "VALIDATION_ERROR")
        self.assertEqual(CartItem.objects.get().quantity, 2)

    def test_patch_cannot_exceed_inventory(self):
        assert_error(self, self.client.patch(self.url, {"quantity": 6}, format="json"),
                     409, "INSUFFICIENT_STOCK")
        self.assertEqual(CartItem.objects.get().quantity, 2)

    def test_patch_item_not_in_cart(self):
        other = make_product("Other")
        for url in [item_url(self.cart_id, other), f"/api/carts/{self.cart_id}/items/garbage/"]:
            assert_error(self, self.client.patch(url, {"quantity": 1}, format="json"),
                         404, "ITEM_NOT_IN_CART")

    def test_delete_removes_line_and_returns_cart(self):
        resp = self.client.delete(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((resp.json()["items"], resp.json()["subtotal"]), ([], 0))
        self.assertEqual(CartItem.objects.count(), 0)

    def test_delete_twice_second_is_404(self):
        self.client.delete(self.url)
        assert_error(self, self.client.delete(self.url), 404, "ITEM_NOT_IN_CART")

    def test_delete_only_touches_its_own_cart(self):
        other_cart = new_cart(self.client)
        add(self.client, other_cart, self.product, 1)
        self.client.delete(self.url)
        self.assertEqual(CartItem.objects.get().cart_id, uuid.UUID(other_cart))


# ------------------------------------------------- live pricing & availability policy
class PriceAndAvailabilityPolicyTests(APITestCase):
    def test_cart_shows_current_price_after_price_change(self):
        p = make_product(price=10000)
        cart_id = new_cart(self.client)
        add(self.client, cart_id, p, 2)
        Product.objects.filter(pk=p.pk).update(unit_price=12000)
        cart = self.client.get(f"/api/carts/{cart_id}/").json()
        self.assertEqual((cart["items"][0]["unit_price"], cart["subtotal"]), (12000, 24000))

    def test_stock_drop_surfaces_warning_and_blocks_readiness(self):
        p = make_product(inventory=5)
        cart_id = new_cart(self.client)
        add(self.client, cart_id, p, 3)
        Product.objects.filter(pk=p.pk).update(inventory=2)
        cart = self.client.get(f"/api/carts/{cart_id}/").json()
        self.assertFalse(cart["checkout_ready"])
        self.assertEqual(cart["warnings"], [{"code": "INSUFFICIENT_STOCK", "product_id": str(p.id),
                                             "requested": 3, "available": 2}])
        # the shopper can fix it by lowering the quantity
        self.client.patch(item_url(cart_id, p), {"quantity": 2}, format="json")
        self.assertTrue(self.client.get(f"/api/carts/{cart_id}/").json()["checkout_ready"])


# ---------------------------------------------------------------- checked-out carts
class CheckedOutCartTests(APITestCase):
    def setUp(self):
        self.product = make_product()
        self.cart_id = new_cart(self.client)
        add(self.client, self.cart_id, self.product, 1)
        cart = Cart.objects.get(pk=self.cart_id)  # simulate what the checkout module does
        self.order = Order.objects.create(cart=cart, subtotal=10000, total=10000)
        Cart.objects.filter(pk=cart.pk).update(status=Cart.Status.CHECKED_OUT)

    def test_all_mutations_are_rejected(self):
        for resp in [add(self.client, self.cart_id, self.product, 1),
                     self.client.patch(item_url(self.cart_id, self.product), {"quantity": 2}, format="json"),
                     self.client.delete(item_url(self.cart_id, self.product))]:
            assert_error(self, resp, 409, "CART_ALREADY_CHECKED_OUT")
        self.assertEqual(CartItem.objects.get().quantity, 1)

    def test_view_still_works_and_links_the_order(self):
        cart = self.client.get(f"/api/carts/{self.cart_id}/").json()
        self.assertEqual((cart["status"], cart["order_id"], cart["checkout_ready"]),
                         ("checked_out", str(self.order.id), False))


# -------------------------------------------------------------- DB-level backstops
class CartConstraintTests(TestCase):
    def test_duplicate_product_line_is_impossible(self):
        p, cart = make_product(), Cart.objects.create()
        CartItem.objects.create(cart=cart, product=p, quantity=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CartItem.objects.create(cart=cart, product=p, quantity=1)

    def test_zero_or_negative_quantity_is_impossible(self):
        p, cart = make_product(), Cart.objects.create()
        for bad in [0, -1]:
            with self.assertRaises(IntegrityError), transaction.atomic():
                CartItem.objects.create(cart=cart, product=p, quantity=bad)


# ------------------------------------------------------------------ concurrency
@skipUnless(connection.vendor == "postgresql", "needs PostgreSQL row-level locking")
class CartConcurrencyTests(TransactionTestCase):
    def test_parallel_adds_of_same_product_merge_without_errors(self):
        p = make_product(inventory=50)
        cart_id = new_cart(APIClient())
        results = run_parallel(lambda i: add(APIClient(), cart_id, p, 1), 8)
        self.assertEqual([r.status_code for r in results], [201] * 8,
                         [r.content for r in results])  # no 500 from the unique constraint
        self.assertEqual(CartItem.objects.get().quantity, 8)  # no lost updates

    def test_parallel_adds_cannot_push_cart_past_inventory(self):
        p = make_product(inventory=3)
        cart_id = new_cart(APIClient())
        results = run_parallel(lambda i: add(APIClient(), cart_id, p, 1), 6)
        self.assertEqual(sorted(r.status_code for r in results), [201] * 3 + [409] * 3)
        self.assertEqual(CartItem.objects.get().quantity, 3)

    def test_parallel_update_and_delete_leave_a_consistent_cart(self):
        p = make_product(inventory=10)
        cart_id = new_cart(APIClient())
        add(APIClient(), cart_id, p, 2)

        def racer(i):
            c = APIClient()
            if i % 2:
                return c.patch(item_url(cart_id, p), {"quantity": 5}, format="json")
            return c.delete(item_url(cart_id, p))

        results = run_parallel(racer, 6)
        self.assertTrue(all(r.status_code in (200, 404) for r in results), [r.content for r in results])
        qty = CartItem.objects.filter(cart_id=cart_id).values_list("quantity", flat=True)
        self.assertEqual(list(qty), [])  # a delete always runs, so the line must end up gone