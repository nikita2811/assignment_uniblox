from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.views import APIView, exception_handler as drf_exception_handler


class ApiError(APIException):
    
    def __init__(self, status_code, code, message, details=None):
        super().__init__(detail=message)
        self.status_code = status_code
        self.code = code
        self.details = details or {}


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:  # unhandled -> let Django return a 500
        return None

    details = {}
    if isinstance(exc, ApiError):
        code, message, details = exc.code, str(exc.detail), exc.details
    elif isinstance(exc, ValidationError):
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        code, message = "VALIDATION_ERROR", "request validation failed"
        details = response.data if isinstance(response.data, dict) else {"non_field_errors": response.data}
    else:
        code = getattr(exc, "default_code", "error").upper()
        message = str(getattr(exc, "detail", exc))
    response.data = {"error": {"code": code, "message": message, "details": details}}
    return response


class PublicAPIView(APIView):
    """Base view: JSON only, no auth (assignment says auth is out of scope), shared error format."""
    authentication_classes = []
    permission_classes = []

    def get_exception_handler(self):
        return exception_handler