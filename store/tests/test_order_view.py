"""Checkout + order tests.   pytest store/tests/test_order_view.py
Needs the cart and products routes (/api/carts/..., /api/admin/products/...) to be included.
Concurrency tests need PostgreSQL (row locks) and are skipped elsewhere."""
import threading
import uuid
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from store.models import Cart, CartItem, Coupon, Order, OrderItem, Product
from store import payment


# ------------------------------------------------------------------- helpers
def make_product(name="Rice", price=10000, inventory=10, sku=None, pack=""):
    return Product.objects.create(name=name, sku=sku or f"T-{uuid.uuid4().hex[:10]}",
                                  product_qty=pack, unit_price=price, inventory=inventory)


def make_coupon(percent=10, code=None, status=Coupon.Status.AVAILABLE):
    return Coupon.objects.create(
        code=code or f"SAVE-{uuid.uuid4().hex[:8].upper()}",
        milestone=(Coupon.objects.count() + 1) * 5, discount_percent=percent, status=status,
        redeemed_at=timezone.now() if status == Coupon.Status.REDEEMED else None)


def new_cart(client, *lines):
    """lines: (product, qty) tuples, added in the order given."""
    cart_id = client.post("/api/carts/", format="json").json()["id"]
    for product, qty in lines:
        resp = client.post(f"/api/carts/{cart_id}/items/",
                           {"product_id": str(product.id), "quantity": qty}, format="json")
        assert resp.status_code == 201, resp.content
    return cart_id


def checkout(client, cart_id, coupon=None, key=None, body=None):
    payload = body if body is not None else ({"coupon_code": coupon} if coupon else {})
    extra = {"HTTP_IDEMPOTENCY_KEY": key} if key is not None else {}
    return client.post(f"/api/carts/{cart_id}/checkout/", payload, format="json", **extra)


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


def inventory(product):
    return Product.objects.get(pk=product.pk).inventory


# ----------------------------------------------------- POST /carts/{id}/checkout/ (success)
class CheckoutSuccessTests(APITestCase):
    def test_checkout_creates_order_decrements_stock_and_closes_cart(self):
        rice = make_product("Rice", 10000, inventory=10, sku="RICE-5KG", pack="5kg")
        cart_id = new_cart(self.client, (rice, 2))
        resp = checkout(self.client, cart_id)
        self.assertEqual(resp.status_code, 201, resp.content)
        order = resp.json()
        self.assertEqual((order["subtotal"], order["discount_amount"], order["total"]), (20000, 0, 20000))
        self.assertEqual((order["coupon_code"], order["discount_percent"]), (None, None))
        self.assertEqual(order["cart_id"], cart_id)
        self.assertTrue(order["payment_reference"].startswith("FAKE-"))
        self.assertEqual(order["items"], [{"product_id": str(rice.id), "sku": "RICE-5KG", "name": "Rice",
                                           "pack_size": "5kg", "unit_price": 10000, "quantity": 2,
                                           "line_total": 20000}])
        self.assertEqual(inventory(rice), 8)
        cart = self.client.get(f"/api/carts/{cart_id}/").json()
        self.assertEqual((cart["status"], cart["order_id"]), ("checked_out", order["id"]))

    def test_multi_item_totals_use_exact_integer_math(self):
        rice, oil = make_product("Rice", 10000), make_product("Oil", 2550)
        order = checkout(self.client, new_cart(self.client, (rice, 2), (oil, 3))).json()
        self.assertEqual({i["name"]: i["line_total"] for i in order["items"]}, {"Rice": 20000, "Oil": 7650})
        self.assertEqual((order["subtotal"], order["total"]), (27650, 27650))
        self.assertEqual(order["subtotal"], sum(i["line_total"] for i in order["items"]))

    def test_buying_exactly_the_remaining_stock_works(self):
        saffron = make_product("Saffron", inventory=3)
        self.assertEqual(checkout(self.client, new_cart(self.client, (saffron, 3))).status_code, 201)
        self.assertEqual(inventory(saffron), 0)

    def test_current_price_is_charged_when_price_changed_after_add(self):
        p = make_product(price=10000)
        cart_id = new_cart(self.client, (p, 2))
        Product.objects.filter(pk=p.pk).update(unit_price=12500)
        order = checkout(self.client, cart_id).json()
        self.assertEqual((order["items"][0]["unit_price"], order["total"]), (12500, 25000))

    def test_order_is_an_immutable_snapshot_of_what_was_bought(self):
        p = make_product("Rice", 10000, sku="RICE", pack="5kg")
        order = checkout(self.client, new_cart(self.client, (p, 2))).json()
        Product.objects.filter(pk=p.pk).update(name="Renamed", unit_price=99999, product_qty="1kg")
        again = self.client.get(f"/api/orders/{order['id']}/").json()
        self.assertEqual(again, order)
        self.assertEqual((again["items"][0]["name"], again["items"][0]["unit_price"],
                          again["items"][0]["pack_size"]), ("Rice", 10000, "5kg"))

    def test_order_total_reconciles_in_the_database(self):
        p = make_product(price=999)
        coupon = make_coupon(10)
        order = Order.objects.get(pk=checkout(self.client, new_cart(self.client, (p, 3)),
                                              coupon=coupon.code).json()["id"])
        self.assertEqual(order.total, order.subtotal - order.discount_amount)
        self.assertEqual(order.subtotal, sum(i.line_total for i in order.items.all()))


# ---------------------------------------------------------- checkout validation & failures
class CheckoutFailureTests(APITestCase):
    def test_empty_cart_cannot_be_checked_out(self):
        assert_error(self, checkout(self.client, new_cart(self.client)), 422, "EMPTY_CART")
        self.assertEqual(Order.objects.count(), 0)

    def test_unknown_or_malformed_cart_is_404(self):
        for cid in [uuid.uuid4(), "garbage"]:
            assert_error(self, checkout(self.client, cid), 404, "CART_NOT_FOUND")

    def test_stock_dropped_after_add_fails_with_details_and_changes_nothing(self):
        a, b = make_product("A", inventory=10), make_product("B", inventory=10)
        cart_id = new_cart(self.client, (a, 3), (b, 4))
        Product.objects.filter(pk=a.pk).update(inventory=2)
        body = assert_error(self, checkout(self.client, cart_id), 409, "INSUFFICIENT_STOCK")
        self.assertEqual(body["details"]["items"],
                         [{"product_id": str(a.id), "requested": 3, "available": 2}])
        self.assertEqual((inventory(a), inventory(b)), (2, 10))  # B untouched: all-or-nothing
        self.assertEqual((Order.objects.count(), Cart.objects.get(pk=cart_id).status), (0, "open"))

    def test_every_short_item_is_reported(self):
        a, b = make_product("A", inventory=5), make_product("B", inventory=5)
        cart_id = new_cart(self.client, (a, 5), (b, 5))
        Product.objects.update(inventory=1)
        body = assert_error(self, checkout(self.client, cart_id), 409, "INSUFFICIENT_STOCK")
        self.assertEqual({i["product_id"] for i in body["details"]["items"]}, {str(a.id), str(b.id)})

    def test_request_body_validation(self):
        cart_id = new_cart(self.client, (make_product(), 1))
        for body in [{"coupon_code": 5}, {"coupon_code": ""}, {"coupon_code": "x" * 33},
                     {"coupon": "SAVE-1"}, [1]]:
            assert_error(self, checkout(self.client, cart_id, body=body), 422, "VALIDATION_ERROR")
        resp = self.client.post(f"/api/carts/{cart_id}/checkout/", data="{bad", content_type="application/json")
        assert_error(self, resp, 400, "PARSE_ERROR")
        self.assertEqual(Order.objects.count(), 0)

    def test_invalid_idempotency_key(self):
        cart_id = new_cart(self.client, (make_product(), 1))
        for key in ["", "   ", "k" * 129]:
            assert_error(self, checkout(self.client, cart_id, key=key), 400, "INVALID_IDEMPOTENCY_KEY")
        self.assertEqual(Order.objects.count(), 0)

    def test_payment_failure_rolls_back_stock_coupon_and_cart(self):
        p, coupon = make_product(inventory=10), make_coupon()
        cart_id = new_cart(self.client, (p, 2))
        with patch.object(payment.gateway, "charge", side_effect=payment.PaymentError("card declined")):
            body = assert_error(self, checkout(self.client, cart_id, coupon=coupon.code), 402, "PAYMENT_FAILED")
        self.assertEqual(body["details"]["reason"], "card declined")
        coupon.refresh_from_db()
        self.assertEqual((coupon.status, coupon.redeemed_at), (Coupon.Status.AVAILABLE, None))
        self.assertEqual((inventory(p), Order.objects.count(), Cart.objects.get(pk=cart_id).status), (10, 0, "open"))
        # the same cart + coupon work once payment recovers
        self.assertEqual(checkout(self.client, cart_id, coupon=coupon.code).status_code, 201)

    def test_checkout_route_only_accepts_post(self):
        cart_id = new_cart(self.client)
        assert_error(self, self.client.get(f"/api/carts/{cart_id}/checkout/"), 405, "METHOD_NOT_ALLOWED")


# ------------------------------------------------------------- idempotency & repeats
class CheckoutIdempotencyTests(APITestCase):
    def test_second_checkout_without_key_never_creates_a_second_order(self):
        p = make_product(inventory=10)
        cart_id = new_cart(self.client, (p, 2))
        first = checkout(self.client, cart_id)
        body = assert_error(self, checkout(self.client, cart_id), 409, "CART_ALREADY_CHECKED_OUT")
        self.assertEqual(body["details"]["order_id"], first.json()["id"])
        self.assertEqual((Order.objects.count(), inventory(p)), (1, 8))

    def test_retry_with_same_key_replays_the_original_order(self):
        p, coupon = make_product(inventory=10), make_coupon()
        cart_id = new_cart(self.client, (p, 2))
        first = checkout(self.client, cart_id, coupon=coupon.code, key="k-1")
        retry = checkout(self.client, cart_id, coupon=coupon.code, key="k-1")
        self.assertEqual((first.status_code, retry.status_code), (201, 200))
        self.assertEqual(retry["Idempotent-Replay"], "true")
        self.assertNotIn("Idempotent-Replay", first)
        self.assertEqual(first.json(), retry.json())
        self.assertEqual((Order.objects.count(), inventory(p)), (1, 8))
        self.assertEqual(Coupon.objects.get(pk=coupon.pk).status, Coupon.Status.REDEEMED)

    def test_replay_is_unaffected_by_later_changes(self):
        p = make_product(inventory=10)
        cart_id = new_cart(self.client, (p, 1))
        first = checkout(self.client, cart_id, key="k")
        Product.objects.filter(pk=p.pk).update(unit_price=1, inventory=0)
        self.assertEqual(checkout(self.client, cart_id, key="k").json(), first.json())

    def test_key_cannot_be_reused_for_a_different_cart_or_coupon(self):
        p = make_product(inventory=10)
        a, b = new_cart(self.client, (p, 1)), new_cart(self.client, (p, 1))
        self.assertEqual(checkout(self.client, a, key="shared").status_code, 201)
        assert_error(self, checkout(self.client, b, key="shared"), 422, "IDEMPOTENCY_KEY_REUSED")
        assert_error(self, checkout(self.client, a, coupon="SAVE-OTHER", key="shared"), 422, "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual((Order.objects.count(), Cart.objects.get(pk=b).status), (1, "open"))

    def test_failed_attempt_does_not_burn_the_key(self):
        p = make_product(inventory=1)
        cart_id = new_cart(self.client, (p, 1))
        Product.objects.filter(pk=p.pk).update(inventory=0)
        assert_error(self, checkout(self.client, cart_id, key="retry-me"), 409, "INSUFFICIENT_STOCK")
        Product.objects.filter(pk=p.pk).update(inventory=5)  # restocked
        self.assertEqual(checkout(self.client, cart_id, key="retry-me").status_code, 201)
        self.assertEqual((Order.objects.count(), inventory(p)), (1, 4))


# ------------------------------------------------------------------------ coupons
class CheckoutCouponTests(APITestCase):
    def test_percentage_discount_is_floored_integer_math(self):
        p = make_product(price=999)
        coupon = make_coupon(10)
        order = checkout(self.client, new_cart(self.client, (p, 1)), coupon=coupon.code).json()
        self.assertEqual((order["subtotal"], order["discount_amount"], order["total"]), (999, 99, 900))
        self.assertEqual((order["coupon_code"], order["discount_percent"]), (coupon.code, 10))
        coupon.refresh_from_db()
        self.assertEqual(coupon.status, Coupon.Status.REDEEMED)
        self.assertIsNotNone(coupon.redeemed_at)
        self.assertEqual(coupon.order.id, uuid.UUID(order["id"]))

    def test_tiny_orders_round_discount_down_to_zero(self):
        p = make_product(price=1)
        order = checkout(self.client, new_cart(self.client, (p, 1)), coupon=make_coupon(10).code).json()
        self.assertEqual((order["discount_amount"], order["total"]), (0, 1))

    def test_hundred_percent_coupon_gives_zero_total_never_negative(self):
        p = make_product(price=12345)
        order = checkout(self.client, new_cart(self.client, (p, 2)), coupon=make_coupon(100).code).json()
        self.assertEqual((order["discount_amount"], order["total"]), (24690, 0))

    def test_coupon_code_is_normalised(self):
        p = make_product()
        coupon = make_coupon(code="SAVE-ABC123")
        order = checkout(self.client, new_cart(self.client, (p, 1)), coupon="  save-abc123 ").json()
        self.assertEqual(order["coupon_code"], "SAVE-ABC123")

    def test_unknown_coupon_has_no_side_effects(self):
        p = make_product()
        cart_id = new_cart(self.client, (p, 1))
        assert_error(self, checkout(self.client, cart_id, coupon="SAVE-NOPE"), 422, "COUPON_NOT_FOUND")
        self.assertEqual((Order.objects.count(), inventory(p), Cart.objects.get(pk=cart_id).status), (0, 10, "open"))

    def test_coupon_can_be_used_only_once(self):
        p = make_product(inventory=10)
        coupon = make_coupon()
        self.assertEqual(checkout(self.client, new_cart(self.client, (p, 1)), coupon=coupon.code).status_code, 201)
        cart_b = new_cart(self.client, (p, 1))
        assert_error(self, checkout(self.client, cart_b, coupon=coupon.code), 409, "COUPON_ALREADY_REDEEMED")
        self.assertEqual((inventory(p), Cart.objects.get(pk=cart_b).status), (9, "open"))
        self.assertEqual(checkout(self.client, cart_b).status_code, 201)  # still buyable without it

    def test_already_redeemed_coupon_is_rejected(self):
        p = make_product()
        coupon = make_coupon(status=Coupon.Status.REDEEMED)
        assert_error(self, checkout(self.client, new_cart(self.client, (p, 1)), coupon=coupon.code),
                     409, "COUPON_ALREADY_REDEEMED")

    def test_failed_checkout_does_not_consume_the_coupon(self):
        p, coupon = make_product(inventory=10), make_coupon()
        cart_id = new_cart(self.client, (p, 5))
        Product.objects.filter(pk=p.pk).update(inventory=1)
        assert_error(self, checkout(self.client, cart_id, coupon=coupon.code), 409, "INSUFFICIENT_STOCK")
        coupon.refresh_from_db()
        self.assertEqual((coupon.status, coupon.redeemed_at), (Coupon.Status.AVAILABLE, None))
        Product.objects.filter(pk=p.pk).update(inventory=10)
        self.assertEqual(checkout(self.client, cart_id, coupon=coupon.code).status_code, 201)


# ----------------------------------------------------------------- GET /orders/{id}/
class OrderRetrievalTests(APITestCase):
    def test_get_order_matches_checkout_response(self):
        p = make_product()
        created = checkout(self.client, new_cart(self.client, (p, 2))).json()
        resp = self.client.get(f"/api/orders/{created['id']}/")
        self.assertEqual((resp.status_code, resp.json()), (200, created))

    def test_unknown_or_malformed_order_is_404(self):
        for oid in [uuid.uuid4(), "garbage"]:
            assert_error(self, self.client.get(f"/api/orders/{oid}/"), 404, "ORDER_NOT_FOUND")

    def test_orders_are_read_only(self):
        p = make_product()
        order_id = checkout(self.client, new_cart(self.client, (p, 1))).json()["id"]
        for method in (self.client.post, self.client.patch, self.client.put, self.client.delete):
            assert_error(self, method(f"/api/orders/{order_id}/", {}, format="json"), 405, "METHOD_NOT_ALLOWED")

    def test_retrieval_does_not_change_state(self):
        p = make_product()
        order_id = checkout(self.client, new_cart(self.client, (p, 1))).json()["id"]
        before = (Order.objects.count(), OrderItem.objects.count(), inventory(p))
        for _ in range(3):
            self.client.get(f"/api/orders/{order_id}/")
        self.assertEqual((Order.objects.count(), OrderItem.objects.count(), inventory(p)), before)


# ------------------------------------------------------------------- concurrency
@skipUnless(connection.vendor == "postgresql", "needs PostgreSQL row-level locking")
class CheckoutConcurrencyTests(TransactionTestCase):
    def test_parallel_checkouts_never_oversell(self):
        p = make_product(inventory=3)
        carts = [new_cart(APIClient(), (p, 1)) for _ in range(8)]
        results = run_parallel(lambda i: checkout(APIClient(), carts[i]), 8)
        self.assertEqual(sorted(r.status_code for r in results), [201] * 3 + [409] * 5)
        self.assertTrue(all(r.json()["error"]["code"] == "INSUFFICIENT_STOCK" for r in results if r.status_code == 409))
        self.assertEqual((inventory(p), Order.objects.count()), (0, 3))
        self.assertEqual(sum(i.quantity for i in OrderItem.objects.all()), 3)

    def test_parallel_retries_with_same_key_create_one_order(self):
        p = make_product(inventory=10)
        cart_id = new_cart(APIClient(), (p, 2))
        results = run_parallel(lambda i: checkout(APIClient(), cart_id, key="same-key"), 6)
        self.assertTrue(all(r.status_code in (200, 201) for r in results), [r.content for r in results])
        self.assertEqual(sum(r.status_code == 201 for r in results), 1)
        self.assertEqual(len({r.json()["id"] for r in results}), 1)
        self.assertEqual((Order.objects.count(), inventory(p)), (1, 8))

    def test_parallel_checkouts_of_one_cart_without_key_create_one_order(self):
        p = make_product(inventory=10)
        cart_id = new_cart(APIClient(), (p, 2))
        results = run_parallel(lambda i: checkout(APIClient(), cart_id), 6)
        self.assertEqual(sorted(r.status_code for r in results), [201] + [409] * 5)
        self.assertTrue(all(r.json()["error"]["code"] == "CART_ALREADY_CHECKED_OUT" for r in results if r.status_code == 409))
        self.assertEqual((Order.objects.count(), inventory(p)), (1, 8))

    def test_parallel_checkouts_with_same_coupon_only_one_wins(self):
        # One product PER cart: otherwise the product row lock would serialise the checkouts and
        # hide a missing coupon lock. Here only the coupon row is contended.
        products = [make_product(f"P{i}", inventory=100) for i in range(5)]
        coupon = make_coupon()
        carts = [new_cart(APIClient(), (products[i], 1)) for i in range(5)]
        results = run_parallel(lambda i: checkout(APIClient(), carts[i], coupon=coupon.code), 5)
        self.assertEqual(sorted(r.status_code for r in results), [201, 409, 409, 409, 409])
        self.assertTrue(all(r.json()["error"]["code"] == "COUPON_ALREADY_REDEEMED" for r in results if r.status_code == 409))
        self.assertEqual(Order.objects.filter(coupon__isnull=False).count(), 1)
        self.assertEqual(Coupon.objects.get(pk=coupon.pk).status, Coupon.Status.REDEEMED)
        self.assertEqual(sum(inventory(p) for p in products), 5 * 100 - 1)  # losers rolled back
        self.assertEqual(Cart.objects.filter(status=Cart.Status.OPEN).count(), 4)

    def test_carts_sharing_products_in_opposite_orders_do_not_deadlock(self):
        a, b = make_product("A", inventory=100), make_product("B", inventory=100)
        carts = [new_cart(APIClient(), *(((a, 1), (b, 1)) if i % 2 else ((b, 1), (a, 1)))) for i in range(8)]
        results = run_parallel(lambda i: checkout(APIClient(), carts[i]), 8)
        self.assertEqual([r.status_code for r in results], [201] * 8, [r.content for r in results])
        self.assertEqual((inventory(a), inventory(b), Order.objects.count()), (92, 92, 8))

    def test_checkout_racing_with_cart_edits_keeps_cart_and_order_in_agreement(self):
        p = make_product(inventory=10)
        cart_id = new_cart(APIClient(), (p, 1))

        def racer(i):
            c = APIClient()
            if i == 0:
                return checkout(c, cart_id)
            return c.post(f"/api/carts/{cart_id}/items/", {"product_id": str(p.id), "quantity": 1}, format="json")

        results = run_parallel(racer, 5)
        self.assertEqual(results[0].status_code, 201)
        ordered = OrderItem.objects.get().quantity
        self.assertEqual(CartItem.objects.get().quantity, ordered)          # late adds were rejected
        self.assertEqual(inventory(p), 10 - ordered)
        self.assertTrue(all(r.status_code in (201, 409) for r in results[1:]))

    def test_checkout_racing_with_inventory_write_off_never_goes_negative(self):
        p = make_product(inventory=5)
        cart_id = new_cart(APIClient(), (p, 5))
        url = f"/api/admin/products/{p.id}/adjust-inventory/"

        def racer(i):
            if i == 0:
                return checkout(APIClient(), cart_id)
            return APIClient().post(url, {"delta": -5}, format="json")

        buy, writeoff = run_parallel(racer, 2)
        self.assertIn((buy.status_code, writeoff.status_code), [(201, 409), (409, 200)])  # exactly one wins
        self.assertEqual(inventory(p), 0)
        self.assertEqual(Order.objects.count(), 1 if buy.status_code == 201 else 0)