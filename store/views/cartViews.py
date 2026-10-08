from django.shortcuts import render
from rest_framework import status
from rest_framework.response import Response
from store.api_errors import PublicAPIView
from store.services import cart_services
from store.serializers.cartSerializers import AddCartItemSerializer, CartSerializer, UpdateCartItemSerializer


def _cart_id(raw):
    return cart_services.parse_uuid_or_404(raw, "CART_NOT_FOUND", "cart not found")


def _product_id(raw):
    return cart_services.parse_uuid_or_404(raw, "ITEM_NOT_IN_CART", "product is not in this cart")


class CartCreateView(PublicAPIView):
    def post(self, request):
        cart = cart_services.create_cart()
        return Response(CartSerializer(cart).data, status=status.HTTP_201_CREATED)


class CartDetailView(PublicAPIView):
    def get(self, request, cart_id):
        return Response(CartSerializer(cart_services.get_cart(_cart_id(cart_id))).data)


class CartItemListView(PublicAPIView):
    def post(self, request, cart_id):
        data = AddCartItemSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        cart = cart_services.add_item(_cart_id(cart_id), data.validated_data["product_id"],
                                 data.validated_data["quantity"])
        return Response(CartSerializer(cart).data, status=status.HTTP_201_CREATED)


class CartItemDetailView(PublicAPIView):
    def patch(self, request, cart_id, product_id):
        data = UpdateCartItemSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        cart = cart_services.set_item_quantity(_cart_id(cart_id), _product_id(product_id),
                                          data.validated_data["quantity"])
        return Response(CartSerializer(cart).data)

    def delete(self, request, cart_id, product_id):
        cart = cart_services.remove_item(_cart_id(cart_id), _product_id(product_id))
        return Response(CartSerializer(cart).data)