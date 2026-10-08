# cart/tests/test_cart_model.py
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from store.models import Cart

pytestmark = pytest.mark.django_db


def test_new_cart_defaults_to_open():
    cart = Cart.objects.create()
    assert cart.status == Cart.Status.OPEN
    assert cart.status == "open"


def test_primary_key_is_a_uuid_generated_automatically():
    cart = Cart.objects.create()
    assert isinstance(cart.id, uuid.UUID)
    assert cart.id.version == 4


def test_each_cart_gets_a_unique_id():
    ids = {Cart.objects.create().id for _ in range(20)}
    assert len(ids) == 20


def test_id_is_not_editable():
    assert Cart._meta.get_field("id").editable is False


def test_created_at_is_set_automatically():
    before = timezone.now()
    cart = Cart.objects.create()
    after = timezone.now()
    assert before <= cart.created_at <= after


def test_created_at_does_not_change_on_save():
    cart = Cart.objects.create()
    original = cart.created_at
    cart.status = Cart.Status.CHECKED_OUT
    cart.save()
    cart.refresh_from_db()
    assert cart.created_at == original


def test_status_choices_are_open_and_checked_out_only():
    assert set(Cart.Status.values) == {"open", "checked_out"}


def test_status_can_transition_to_checked_out_and_persists():
    cart = Cart.objects.create()
    cart.status = Cart.Status.CHECKED_OUT
    cart.save()
    assert Cart.objects.get(pk=cart.pk).status == "checked_out"


def test_invalid_status_fails_validation():
    cart = Cart(status="banana")
    with pytest.raises(ValidationError) as exc:
        cart.full_clean()
    assert "status" in exc.value.message_dict


def test_status_longer_than_max_length_fails_validation():
    cart = Cart(status="x" * 21)
    with pytest.raises(ValidationError):
        cart.full_clean()


def test_valid_cart_passes_full_clean():
    Cart(status=Cart.Status.OPEN).full_clean()
    Cart(status=Cart.Status.CHECKED_OUT).full_clean()


def test_conditional_update_only_one_caller_wins_open_to_checked_out():
    """The pattern checkout should rely on: flip OPEN -> CHECKED_OUT
    atomically; the second attempt must affect 0 rows."""
    cart = Cart.objects.create()

    first = Cart.objects.filter(pk=cart.pk, status=Cart.Status.OPEN).update(
        status=Cart.Status.CHECKED_OUT
    )
    second = Cart.objects.filter(pk=cart.pk, status=Cart.Status.OPEN).update(
        status=Cart.Status.CHECKED_OUT
    )

    assert first == 1
    assert second == 0