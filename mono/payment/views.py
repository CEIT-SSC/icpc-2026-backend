# payment/views.py

from django.conf import settings
from django.http import HttpResponseRedirect
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, OpenApiResponse

from .serializers import (
    VerifySerializer,
    PaymentSerializer, StartPaymentSerializer,
)
from .services import process_gateway_callback, verify_by_authority, startpay


def _frontend_return_url(**params):
    """Add callback results without breaking an existing query string."""
    parts = urlsplit(settings.PAYMENT_FRONTEND_RETURN)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items() if value is not None})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class VerifyPaymentView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=VerifySerializer,
        responses={
            200: PaymentSerializer,
            401: OpenApiResponse(description="Unauthenticated or invalid/foreign authority"),
        },
        description="Verify a payment by authority (frontend sends authority after the gateway redirect)."
    )
    def post(self, request):
        s = VerifySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        p = verify_by_authority(user=request.user, authority=s.validated_data["authority"])
        return Response(PaymentSerializer(p).data, status=status.HTTP_200_OK)


class CallbackView(APIView):
    permission_classes = []

    @extend_schema(
        request=None,
        responses={302: OpenApiResponse(description="Redirects to frontend with ?authority=...")},
        description="Gateway callback. Verifies the payment server-side, then redirects to the frontend."
    )
    def get(self, request):
        authority = request.GET.get("Authority")
        gateway_status = request.GET.get("Status", "")
        if not authority:
            return HttpResponseRedirect(_frontend_return_url(status="invalid_callback"))

        payment = process_gateway_callback(
            authority=authority,
            gateway_status=gateway_status,
        )
        return HttpResponseRedirect(
            _frontend_return_url(authority=authority, status=payment.status.lower())
        )


class StartpaymentView(APIView):
    permission_classes = []
    @extend_schema(
        parameters=[StartPaymentSerializer],
        responses={302: OpenApiResponse(description="Redirects to new payment page")}
    )
    def get(self, request):
        authority = request.GET.get("authority")
        redirection_url = startpay(authority)
        return HttpResponseRedirect(redirection_url)
