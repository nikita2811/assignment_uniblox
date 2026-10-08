from rest_framework import status
from rest_framework.exceptions import APIException


class ProductNotFound(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    default_detail = "Product not found."
    default_code = "product_not_found"