# Checkout & Rewards Service

Backend for an ecommerce store: carts, idempotent checkout, orders, and an "every nth order earns a coupon" reward system. Built with **Django + Django REST Framework + PostgreSQL**.

The interesting part is not the CRUD, it is how the system behaves under retries, concurrent checkouts, and competing coupon redemptions. See [`DECISIONS.md`](./DECISIONS.md) for the invariants, trade-offs and what was deferred.

> Time spent: ~ 5-6 hours.

---

## Stack

| Concern | Choice |
|---|---|
| Framework | Django + Django REST Framework |
| Database | PostgreSQL (`psycopg`) |
| Config | `django-environ` / `python-dotenv` (`.env`) |
| Server | `gunicorn` |
| Tests | `pytest`, `pytest-django` |
| CI | `github actions` |

## Setup

### 1. Prerequisites
- Python 3.11+
- PostgreSQL running locally (tests for concurrency need a real DB; SQLite does not give row-level locking)

### 2. Install
```bash
git clone https://github.com/nikita2811/assignment_uniblox.git
cd assignment_uniblox
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure
Create a `.env` in the project root ( verify variable names against `settings.py`):
```env
DB_NAME=ecom
DB_USER=postgres
DB_PASSWORD=root
DB_HOST=localhost
DB_PORT=5432
DJANGO_SETTINGS_MODULE=assignment_uniblox.settings
```

### 4. Migrate and seed
```bash
createdb uniblox
python manage.py migrate
python manage.py seed  (used flag --reset-stock for restocking products and inventory)   #  management command used for seeding data
```
Seed data: at least 10 products, including one with limited inventory.

### 5. Run
```bash
python manage.py runserver
```

### 6. Test
```bash
pytest
```
Includes tests for repeated checkout (same idempotency key), concurrent checkout over limited stock, and concurrent coupon redemption.testcases for cart,orders and checkout,product followed TDD apporch

---

## API overview

Base path: `/api` ⚠️ VERIFY. All bodies are JSON. Money is returned as integer minor units (cents) alongside a currency code.

| Method | Path | Purpose | Success | Notable errors |
|---|---|---|---|---|
| GET | `/products/` | List products | 200 | |
| POST | `/carts/` | Create a cart | 201 | |
| GET | `/carts/{id}/` | View cart with line prices and totals | 200 | 404 |
| POST | `/carts/{id}/items/` | Add item `{product_id, quantity}` | 201 | 400 invalid quantity, 404 unknown product, 409 cart already checked out |
| PATCH | `/carts/{id}/items/{product_id}/` | Change quantity | 200 | 400, 404, 409 |
| DELETE | `/carts/{id}/items/{product_id}/` | Remove item | 204 | 404, 409 |
| POST | `/carts/{id}/checkout/` | Check out, optional `{coupon_code}`. Requires `Idempotency-Key` header | 201 (first), 200 (replay) | 400 missing key, 404, 409 conflict, 422 coupon/stock problems |
| GET | `/orders/{id}/` | Retrieve an order snapshot | 200 | 404 |
| POST | `/admin/coupons/generate/` | **Admin**: generate coupon if a milestone is unrewarded | 201 | 409 no eligible milestone |
| GET | `/admin/report/` | **Admin**: read-only summary | 200 | |

**Administrative operations** (no auth implemented, by design): `/admin/coupons/generate/` and `/admin/report/`.

### Example flow
```bash
# get products
curl -X GET localhost/api/products

# create cart
curl -X POST localhost:8000/api/carts/

#  add item
curl -X POST localhost:8000/api/carts/$CART/items/ \
  -H 'Content-Type: application/json' \
  -d '{"product_id": 1, "quantity": 2}'

# checkout (safe to retry with the same key)
curl -X POST localhost:8000/api/carts/$CART/checkout/ \
  -H 'Idempotency-Key: 7d1c0d4e-1b6f-4d0e-9c59-0c3e2c1a9a11' \
  -H 'Content-Type: application/json' \
  -d '{"coupon_code": "OPTIONAL"}'

#  admin
curl -X POST localhost:8000/api/admin/coupons/generate/
curl localhost:8000/api/admin/report/
```

### Error format
Every error returns a stable machine-readable `code` plus a human message:
```json
{ "error": { "code": "insufficient_stock", "message": "Only 1 unit(s) of 'Limited Sneaker' available.", "details": { "product_id": 3, "available": 1, "requested": 2 } } }
```
Codes (⚠️ VERIFY against implementation): `validation_error`, `product_not_found`, `cart_not_found`, `cart_already_checked_out`, `cart_empty`, `insufficient_stock`, `coupon_invalid`, `coupon_already_redeemed`, `idempotency_key_required`, `idempotency_key_reused`, `no_eligible_milestone`.

---

## Project layout
```
assignment_uniblox/   Django project (settings, urls, wsgi)
store/                App: models, services, views, serializers, tests
DECISIONS.md          Design decisions and trade-offs
pytest.ini            pytest-django configuration
requirements.txt
.github/workflows/ci.yml  for automated testing
```

## Known limitations
See "Implemented vs deferred" in [`DECISIONS.md`](./DECISIONS.md).