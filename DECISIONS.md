# DECISIONS

Stack: Django + Django REST Framework + PostgreSQL. Money is stored as integer minor units.
Approch : Test Driven Developement first wrote test cases then started development
Automated Testing : configured github actions for CI contiues integration
Time spent: 5-6 hours.

 
---

## 1. System invariants

| # | Invariant | Where enforced |
|---|---|---|
| I1 | Stock never goes negative; units sold never exceed available inventory. | Product row locked (`select_for_update`) during checkout; `CHECK (inventory >= 0)` as a backstop. |
| I2 | A cart produces at most one order. | Cart row lock + status check; unique order-per-cart constraint. |
| I3 | A retried checkout (same `Idempotency-Key`) returns the original order and never decrements stock or redeems a coupon again. | Key stored on the order (unique), checked inside the transaction. |
| I4 | A coupon is redeemed at most once. | Coupon row lock + status check; unique link coupon → order. |
| I5 | A failed checkout never consumes or loses a coupon. | Redemption happens in the same transaction as the order; rollback restores it. |
| I6 | At most one coupon per milestone. | Unique constraint on `milestone`. |
| I7 | `total = subtotal - discount`, and `0 <= discount <= subtotal`, so a total is never negative. | Computed once in the service layer, stored on the order. |
| I8 | An order is a snapshot: names, unit prices, quantities and totals never change after creation. | Order lines copy product data; reports read orders, not products. |
| I9 | The report reconciles: `gross - discounts = net`; figures equal sums over orders and coupons; reading it mutates nothing. | Single read-only `REPEATABLE READ` transaction. |

## 2. Ambiguities and the semantics chosen

| Question | Choice |
|---|---|
| Product price changes after an item is in the cart | Carts store **no price**. Cart view shows the **live** price; checkout charges the live price and snapshots it on the order. The customer sees what they will pay. |
| Availability changes after adding | Adding to a cart **does not reserve** stock. Stock is verified and decremented only at checkout. Checkout fails with a list of the offending items if short. |
| Adding a product already in the cart | **Merge**: quantities are added to the existing line. |
| Invalid quantity or product | Rejected with a clear 4xx error; nothing enters the cart silently. |
| What counts toward the nth order | Only successfully placed orders. Milestones are at order counts `n, 2n, 3n, …`. |
| Coupons per order | **One** coupon per checkout. |
| Coupon ownership | **Not tied to a customer** (no user model); whoever holds the code can use it, once. |
| Coupon discount base | Percentage `x` of the whole cart subtotal. |
| Missed milestones | Generation **catches up one milestone per call**: each call creates the coupon for the oldest unrewarded eligible milestone. |
| Invalid / used coupon at checkout | The whole checkout is rejected; the coupon is never silently ignored. |
| Coupon expiry | None (deferred). |
| Payment | A fake payment step that always succeeds; successful validation = payment success. |

## 3. Material design decisions

### Decision 1: Pessimistic row locks vs optimistic versioning
**Context:** Concurrent checkouts compete for limited stock and for the same coupon.
**Options considered:** (a) optimistic version column with retry; (b) conditional `UPDATE … WHERE inventory >= n`; (c) `SELECT … FOR UPDATE` inside one transaction.
**Choice:** (c), row locks on cart, products and coupon.
**Why:** The checkout touches several rows that must change together (stock, coupon, order). Locks make the invariant easy to reason about and to test; optimistic retries add loop logic and fairness questions for a hot, limited-stock product.
**Consequences:** Correct across multiple app instances because the lock lives in Postgres. Throughput on a single hot product is bounded by lock-hold time. Concurrency tests require PostgreSQL (SQLite has no row locks).

### Decision 2: Idempotency key stored on the order vs a separate table
**Context:** Clients retry on timeout; a retry must not create a second order.
**Options considered:** separate `IdempotencyRecord` table storing responses; key stored directly on the order with a unique constraint; rely on "cart already checked out".
**Choice:** Store the key on the order (unique). A replay returns the stored order with the same body; the same key with a different payload is rejected.
**Why:** "Cart already checked out" alone cannot tell a retry of the winning request (should succeed) from a genuinely different second attempt (should fail). Keeping the key on the order means the key and the order commit atomically, so there is no state where one exists without the other.
**Consequences:** Simple and consistent. Only successful checkouts are remembered; failed attempts are naturally retryable. No key expiry (deferred).

### Decision 3: Deterministic lock order
**Context:** Two carts sharing products could deadlock if they lock rows in different orders.
**Choice:** Always lock in the same order: cart, then products sorted by id, then coupon.
**Why:** A global ordering removes circular waits.
**Consequences:** Deadlocks are avoided by construction. Tests deliberately break the ordering/locks to confirm they fail.

### Decision 4: Integer minor units, explicit rounding
**Context:** Floats drift and break reconciliation.
**Options considered:** `float`; `Decimal`; integer minor units.
**Choice:** Integer minor units (paise/cents) in DB and API.
**Why:** Exact arithmetic, trivial reconciliation of report sums.
**Consequences:** Single currency. Discount uses floor division (see §5). Column width matters (see §7, overflow).

### Decision 5: Fake payment inside the transaction vs outbox/two-phase
**Context:** No real payment is required, but the design should not preclude one.
**Options considered:** call a fake gateway inside the transaction; outbox/pending-order two-phase flow.
**Choice:** A fake payment that always succeeds, called inside the checkout transaction.
**Why:** It is instantaneous and local, so it cannot hold locks for long. A real gateway must **not** sit inside a lock-holding transaction.
**Consequences:** Simple now; documented evolution path in §8.

### Decision 6: Coupon redeemed inside the checkout transaction
**Context:** A coupon must not be lost by a failed checkout or used twice.
**Options considered:** mark used up front and release on failure; reservation with expiry; lock + redeem at the end of the order transaction.
**Choice:** Lock the coupon row and mark it redeemed at the end of the same transaction.
**Why:** Rollback restores it automatically; there is no cleanup path to forget. Two parallel checkouts serialize on the coupon row and the loser fails with `COUPON_ALREADY_REDEEMED`, rolling back its stock changes.
**Consequences:** Strong guarantees; no multi-step reservation.

### Decision 7: Milestone-keyed coupon generation
**Context:** Admin may call generate repeatedly or concurrently.
**Options considered:** compare coupon count to `orders // n` in app code; unique `milestone` column.
**Choice:** Each coupon stores its `milestone` (unique). Eligible milestones = `orders // n`; generation creates the lowest milestone with no coupon. A concurrent duplicate hits the unique constraint and is reported as "nothing eligible".
**Why:** The database enforces once-per-milestone even if app logic races.
**Consequences:** Generation is idempotent in effect. `n` and `x` come from settings/env.

### Decision 8: Consistent-snapshot report
**Context:** Several separate queries let an order land mid-request, so totals could disagree.
**Choice:** Run the whole report in one read-only `REPEATABLE READ` transaction; no writes.
**Why:** Guarantees I9 under concurrent activity and satisfies "repeated report requests must not mutate state".
**Consequences:** Slightly longer-lived read transaction; negligible at this scale.

## 4. Transaction, concurrency and idempotency strategy

Checkout, in one `transaction.atomic()`:
1. If an order with this `Idempotency-Key` exists, return it (replay) or reject if the payload differs.
2. Lock the cart; reject if already checked out or empty.
3. Lock products (sorted by id); verify stock for every line, collecting all shortages.
4. If a `coupon_code` is given, lock it and verify it exists and is unredeemed.
5. Compute subtotal, discount and total with integer arithmetic.
6. Fake payment succeeds.
7. Decrement stock, create order and lines, mark coupon redeemed, mark cart checked out.
8. Commit; any exception rolls back everything.

Backstops in the schema: stock `CHECK >= 0`, unique idempotency key, unique `milestone`, one order per cart, one redemption per coupon.

Tests: parallel checkouts competing for the same limited stock, parallel checkouts racing for one coupon (5 threads), repeated checkout with the same key, and parallel coupon generation. Locks were removed on purpose to confirm each test fails without them.(earlier session reported 31 cart tests, 38 checkout/order tests, 5 concurrency tests).

## 5. Money and rounding

- All amounts are integer minor units.
- `discount = floor(subtotal × x / 100)` (integer floor division, deterministic).
- `total = subtotal − discount`; discount is capped at subtotal, so total ≥ 0.
- Discount and total are computed once at checkout and stored; they are never recomputed from live data.

## 6. Error model

Every error returns a stable machine-readable `code` plus a message; clients branch on `code`.

| Situation | Status | Code |
|---|---|---|
| Unknown cart / product / order | 404 | `NOT_FOUND`  |
| Bad quantity, missing fields | 422 | validation error |
| Empty cart at checkout | 422 | `CART_EMPTY` |
| Insufficient stock (lists offending items) | 409 | `INSUFFICIENT_STOCK` |
| Cart already checked out | 409 | `CART_ALREADY_CHECKED_OUT` |
| Coupon already redeemed | 409 | `COUPON_ALREADY_REDEEMED` |
| Invalid coupon | 404 / 422 | `COUPON_INVALID` |
| Missing idempotency key, or reused with a different payload | 400 | `IDEMPOTENCY_KEY_*` |
| No eligible milestone for generation | 409 | `NO_ELIGIBLE_MILESTONE` |
| Order too large for the column | 422 | `ORDER_TOTAL_TOO_LARGE` (once fixed) |

## 7. Implemented vs deferred

**Implemented**
- Products with admin create/update/inventory adjustment; seed data (6 products, one with inventory 3).
- Cart create/view/add/update/remove with live prices and totals.
- Atomic, idempotent checkout with order snapshot, coupon redemption and fake payment.
- Admin coupon generation (n, x from config) and admin report.
- Concurrency and retry tests on PostgreSQL.

**Known open issues (state honestly)**
- **Overflow:** order money columns are 32-bit; 50 units at the maximum price raises `DataError` (500). Fix: change order money fields to `BigIntegerField` or reject with 422 `ORDER_TOTAL_TOO_LARGE`.  Mark as fixed only if you fixed it.
- `Product._generate_sku` could loop forever on repeated collisions; bound the retries or use a DB-generated value.
- Concurrency tests are skipped on SQLite; run on PostgreSQL (docker-compose provided). compose file exists.

**Deferred**
- Authentication/authorization (admin routes are marked `/api/admin/…` only).
- Stock reservation while in cart; coupon expiry, customer binding, stacking.
- Idempotency key expiry; real payments, refunds, cancellations.
- Pagination, multi-currency, tax, observability.

## 8. Evolution: multiple instances and production scale

- Locking is already in PostgreSQL, so several app instances behind a load balancer remain correct without extra coordination.
- **Real payments:** move to two phases. Transaction 1 reserves stock and creates a `pending` order; call the gateway outside the transaction using the order id as the gateway idempotency key; transaction 2 confirms or releases. Add a reaper for stuck `pending` orders and an outbox for events.
- **Hot products:** replace row locks with conditional atomic updates or a reservation table to shorten lock time.
- **Reporting:** read replica or pre-aggregated tables; keep the snapshot guarantee.
- **Milestones:** a counter row instead of `COUNT(*)` at high volume.
- Add connection pooling, rate limiting, an admin auth role, key expiry, and metrics on lock waits and conflict rates.

## 9. How AI tools were used

> ✍️ Edit so this reflects what *you* did. Concrete examples from this project (confirm they match your experience):

- AI generated the cart, checkout and admin modules and test scaffolding in DRF; I reviewed behavior by running the suite on PostgreSQL rather than trusting it.
- **Bug caught by a test:** a DRF `BooleanField` mishandled a query-string flag; a failing test exposed it and it was corrected.
- **Weak concurrency tests strengthened (corrected AI output):**
  1. A test used one shared product for every cart, so the product lock masked a missing coupon lock. I rewrote it so carts use distinct products and only the coupon is contended.
  2. A "lock-order" mutation test used a different but still *consistent* order, so it could never deadlock and proved nothing. I redesigned it to actually break the ordering.
- A later check found the 32-bit overflow for very large orders, which the AI had not initially considered.

## 10. What I would examine first with two more hours

1. Fix and test the integer overflow (`BigIntegerField` or `ORDER_TOTAL_TOO_LARGE`).
2. A larger stress test (many threads, shared stock and coupons) asserting all invariants I1–I9, including report reconciliation.
3. Bound or replace the SKU generation loop.
4. Add admin authentication and rate limiting.
5. Idempotency key expiry and payload-hash tests.
6. Review report cost on large order tables.