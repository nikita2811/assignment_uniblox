from rest_framework import status
from rest_framework.response import Response

from store.api_errors import PublicAPIView

from store.services.product_service import services
from store.serializers.productSerializers import (InventoryAdjustSerializer, ProductCreateSerializer, ProductQuerySerializer,
                          ProductSerializer, ProductUpdateSerializer)


class ProductListView(PublicAPIView):
    def get(self, request):
        # .dict(): with a QueryDict DRF treats a missing BooleanField as False (HTML-form semantics),
        # which would silently turn "no filter" into in_stock=false.
        query = ProductQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        products = services.list_products(query.validated_data.get("in_stock"),
                                          query.validated_data.get("search"))
        data = ProductSerializer(products, many=True).data
        return Response({"count": len(data), "results": data})


class ProductDetailView(PublicAPIView):
    def get(self, request, product_id):
        return Response(ProductSerializer(services.get_product(product_id)).data)


# ----- administrative operations (auth intentionally not implemented)
class AdminProductCreateView(PublicAPIView):
    def post(self, request):
        data = ProductCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        product = services.create_product(data.validated_data)
        return Response(ProductSerializer(product).data, status=status.HTTP_201_CREATED)


class AdminProductDetailView(PublicAPIView):
    def patch(self, request, product_id):
        data = ProductUpdateSerializer(data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        return Response(ProductSerializer(services.update_product(product_id, data.validated_data)).data)


class AdminProductInventoryView(PublicAPIView):
    def post(self, request, product_id):
        data = InventoryAdjustSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        product = services.adjust_inventory(product_id, data.validated_data["delta"])
        return Response(ProductSerializer(product).data)