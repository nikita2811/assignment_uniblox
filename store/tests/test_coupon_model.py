import threading

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.utils import timezone

from store.models import Coupon
from store.money import calculate_discount

pytestmark = pytest.mark.django_db


def make_coupon(code="SAVE-A", milestone=5, percent=10, **kw):
    return Coupon.objects.create(
        code=code, milestone=milestone, discount_percent=percent, **kw
    )


# ---------------- defaults ----------------

def test_defaults_on_new_coupon():
    c = make_coupon()
    assert c.status == Coupon.Status.AVAILABLE
    assert c.redeemed_at is None
    assert c.created_at is not None


def test_code_is_auto_generated_and_unique_when_not_supplied():
    a = Coupon.objects.create(milestone=5, discount_percent=10)
    b = Coupon.objects.create(milestone=10, discount_percent=10)
    assert a.code.startswith("SAVE-") and len(a.code) <= 32
    assert a.code != b.code


def test_created_at_does_not_change_on_save():
    c = make_coupon()
    original = c.created_at
    c.discount_percent = 15
    c.save()
    c.refresh_from_db()
    assert c.created_at == original


def test_status_choices_are_available_and_redeemed_only():
    assert set(Coupon.Status.values) == {"available", "redeemed"}


# ---------------- uniqueness ----------------

def test_duplicate_code_is_rejected():
    make_coupon(code="DUP", milestone=5)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_coupon(code="DUP", milestone=10)
    assert Coupon.objects.count() == 1


def test_duplicate_milestone_is_rejected():
    make_coupon(code="A", milestone=5)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_coupon(code="B", milestone=5)
    assert Coupon.objects.count() == 1


def test_distinct_code_and_milestone_allowed():
    make_coupon(code="A", milestone=5)
    make_coupon(code="B", milestone=10)
    assert Coupon.objects.count() == 2


# ---------------- DB constraints ----------------

@pytest.mark.parametrize("percent", [0, 101, 1000])
def test_discount_percent_out_of_range_is_rejected(percent):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_coupon(percent=percent)


@pytest.mark.parametrize("percent", [1, 10, 100])
def test_discount_percent_boundaries_accepted(percent):
    make_coupon(percent=percent)


def test_negative_milestone_or_percent_is_rejected():
    # PositiveIntegerField / PositiveSmallIntegerField add DB check constraints
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_coupon(code="N1", milestone=-5)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_coupon(code="N2", milestone=5, percent=-10)


def test_redeemed_requires_redeemed_at():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_coupon(status="redeemed", redeemed_at=None)


def test_available_must_not_have_redeemed_at():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_coupon(status="available", redeemed_at=timezone.now())


def test_redeemed_with_timestamp_accepted():
    make_coupon(status="redeemed", redeemed_at=timezone.now())


def test_required_fields_cannot_be_null():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Coupon.objects.create(code="N", milestone=None, discount_percent=10)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Coupon.objects.create(code="N", milestone=5, discount_percent=None)


# ---------------- full_clean ----------------

def test_full_clean_rejects_invalid_status():
    c = Coupon(code="X", milestone=5, discount_percent=10, status="banana")
    with pytest.raises(ValidationError) as exc:
        c.full_clean()
    assert "status" in exc.value.message_dict


def test_full_clean_rejects_long_code():
    c = Coupon(code="X" * 33, milestone=5, discount_percent=10)
    with pytest.raises(ValidationError) as exc:
        c.full_clean()
    assert "code" in exc.value.message_dict


def test_full_clean_accepts_valid_coupon():
    Coupon(code="OK", milestone=5, discount_percent=10).full_clean()


# ---------------- redemption semantics ----------------

def test_conditional_redeem_only_first_caller_wins():
    c = make_coupon()
    now = timezone.now()
    first = Coupon.objects.filter(code=c.code, status="available").update(
        status="redeemed", redeemed_at=now
    )
    second = Coupon.objects.filter(code=c.code, status="available").update(
        status="redeemed", redeemed_at=timezone.now()
    )
    c.refresh_from_db()
    assert (first, second) == (1, 0)
    assert c.status == "redeemed"
    assert c.redeemed_at == now


def test_redeeming_unknown_code_affects_zero_rows():
    assert Coupon.objects.filter(code="NOPE", status="available").update(
        status="redeemed", redeemed_at=timezone.now()
    ) == 0


def test_rolled_back_redeem_leaves_coupon_available():
    c = make_coupon()

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with transaction.atomic():
            Coupon.objects.filter(pk=c.pk, status="available").update(
                status="redeemed", redeemed_at=timezone.now()
            )
            raise Boom()

    c.refresh_from_db()
    assert c.status == "available"
    assert c.redeemed_at is None


# ---------------- discount calculation ----------------

@pytest.mark.parametrize(
    "subtotal, percent, expected",
    [
        (100000, 10, 10000),   # exact
        (999, 10, 99),         # floor: 99.9 -> 99
        (1, 10, 0),            # tiny subtotal -> 0, never negative
        (0, 10, 0),            # empty/zero subtotal
        (12345, 100, 12345),   # 100% => total becomes 0, not below
        (3 * 3333, 15, 1499),  # 9999 * 15 // 100 = 1499
    ],
)
def test_calculate_discount(subtotal, percent, expected):
    assert calculate_discount(subtotal, percent) == expected


@pytest.mark.parametrize("subtotal", [0, 1, 7, 99, 100, 12345, 10**9])
@pytest.mark.parametrize("percent", [1, 10, 33, 99, 100])
def test_discount_never_exceeds_subtotal_or_goes_negative(subtotal, percent):
    d = calculate_discount(subtotal, percent)
    assert isinstance(d, int)
    assert 0 <= d <= subtotal
    assert subtotal - d >= 0


def test_discount_is_deterministic():
    assert {calculate_discount(12345, 17) for _ in range(50)} == {2098}


# ---------------- competing operations (real Postgres) ----------------

def _run_parallel(worker, n):
    barrier = threading.Barrier(n)
    results = []

    def target():
        try:
            barrier.wait()
            results.append(worker())
        except Exception as e:                      # noqa: BLE001
            results.append(e)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=target) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.mark.django_db(transaction=True)
def test_concurrent_redeem_of_same_coupon_has_exactly_one_winner():
    c = make_coupon(code="ONCE", milestone=5)

    def redeem():
        with transaction.atomic():
            return Coupon.objects.filter(code="ONCE", status="available").update(
                status="redeemed", redeemed_at=timezone.now()
            )

    results = _run_parallel(redeem, 10)

    assert all(isinstance(r, int) for r in results), results
    assert sorted(results) == [0] * 9 + [1]
    c.refresh_from_db()
    assert c.status == "redeemed"


@pytest.mark.django_db(transaction=True)
def test_concurrent_creation_for_same_milestone_has_exactly_one_winner():
    def create():
        try:
            with transaction.atomic():
                Coupon.objects.create(milestone=5, discount_percent=10)
            return "ok"
        except IntegrityError:
            return "conflict"

    results = _run_parallel(create, 8)

    assert results.count("ok") == 1
    assert results.count("conflict") == 7
    assert Coupon.objects.filter(milestone=5).count() == 1