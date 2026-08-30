from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from .models import Payment


@override_settings(
    PAYMENT_FRONTEND_RETURN="https://frontend.example/payment/status/",
    ZARINPAL_MERCHANT_ID="merchant-id",
)
class CallbackTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="payer@example.com", password="password"
        )
        self.payment = Payment.objects.create(
            user=self.user,
            target_type=Payment.TargetType.COURSE,
            target_id="",
            amount=10000,
            authority="A000000000000000000000000000001",
        )

    @patch("payment.services.on_payment_success")
    @patch("payment.services._verify_payment")
    def test_callback_verifies_without_frontend_authentication(self, verify, success_hook):
        verify.return_value = {
            "data": {"code": 100, "ref_id": 123, "message": "Paid"}
        }

        response = self.client.get(
            "/api/payment/callback/",
            {"Authority": self.payment.authority, "Status": "OK"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            "https://frontend.example/payment/status/"
            f"?authority={self.payment.authority}&status=successful",
        )
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.SUCCESSFUL)
        success_hook.assert_called_once()

    @patch("payment.services.on_payment_failure")
    def test_cancelled_callback_marks_payment_failed(self, failure_hook):
        response = self.client.get(
            "/api/payment/callback/",
            {"Authority": self.payment.authority, "Status": "NOK"},
        )

        self.assertEqual(response.status_code, 302)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.FAILED)
        failure_hook.assert_called_once()
