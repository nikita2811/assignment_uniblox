"""Small shared helpers (strict input fields, unknown-field rejection, id parsing)."""
import uuid

from rest_framework import serializers

from store.api_errors import ApiError


class StrictIntegerField(serializers.IntegerField):
    """Only real JSON integers: rejects "2", 2.5, true."""

    def to_internal_value(self, data):
        if isinstance(data, bool) or not isinstance(data, int):
            self.fail("invalid")
        return super().to_internal_value(data)


class StrictCharField(serializers.CharField):
    """Only real JSON strings: DRF's CharField would turn 123 into "123"."""

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class RejectUnknownFieldsMixin:
    """A typo like {"price": 5} must fail loudly instead of being silently ignored."""

    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            unknown = sorted(set(data.keys()) - set(self.fields))
            if unknown:
                raise serializers.ValidationError({k: ["Unknown field."] for k in unknown})
        return super().to_internal_value(data)


def parse_uuid_or_404(raw, code, message):
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        raise ApiError(404, code, message)