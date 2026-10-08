from rest_framework import status
from rest_framework.response import Response

from store.api_errors import PublicAPIView

from store.services.coupon_service import services
from store.serializers.AdminCouponSerializers import CouponQuerySerializer, CouponSerializer


# All views here are ADMINISTRATIVE. Authentication is out of scope for the assignment; in
# production they would sit behind admin-only permissions (and the /api/admin/ prefix).
class CouponGenerateView(PublicAPIView):
    def post(self, request):
        coupon = services.generate_coupon()
        return Response(CouponSerializer(coupon).data, status=status.HTTP_201_CREATED)


class CouponListView(PublicAPIView):
    def get(self, request):
        # .dict(): see ProductListView; keeps DRF from applying HTML-form semantics to query strings
        query = CouponQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        coupons = services.list_coupons(query.validated_data.get("status"))
        data = CouponSerializer(coupons, many=True).data
        return Response({"count": len(data), "results": data})


class ReportView(PublicAPIView):
    def get(self, request):
        return Response(services.build_report())