from rest_framework.response import Response

from store.api_errors import ApiError, PublicAPIView
from store.common import parse_uuid_or_404

from store.services import order_service
from store.serializers.ordersSerializers import CheckoutSerializer, OrderSerializer


def _idempotency_key(request):
    raw = request.headers.get("Idempotency-Key")
    if raw is None:
        return None
    key = raw.strip()
    if not key or len(key) > 128:
        raise ApiError(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be 1-128 characters")
    return key


class CartCheckoutView(PublicAPIView):
    def post(self, request, cart_id):
        data = CheckoutSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        cart_uuid = parse_uuid_or_404(cart_id, "CART_NOT_FOUND", "cart not found")
        order, replayed = order_service.checkout(cart_uuid, data.validated_data.get("coupon_code"),
                                            _idempotency_key(request))
        response = Response(OrderSerializer(order).data, status=200 if replayed else 201)
        if replayed:
            response["Idempotent-Replay"] = "true"
        return response


class OrderDetailView(PublicAPIView):
    def get(self, request, order_id):
        return Response(OrderSerializer(order_service.get_order(order_id)).data)