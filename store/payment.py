"""Tiny payment abstraction. The fake always succeeds; swap `gateway` for a real client later.
`idempotency_key` is passed through so a real gateway can dedupe a re-sent charge."""
import uuid


class PaymentError(Exception):
    """Raised when the gateway declines or fails to charge."""


class FakePaymentGateway:
    def charge(self, amount, idempotency_key=None):
        return f"FAKE-{uuid.uuid4().hex[:16].upper()}"


gateway = FakePaymentGateway()