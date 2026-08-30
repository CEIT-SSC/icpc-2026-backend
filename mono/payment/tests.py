from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from presentations.models import Course, Registration
from .models import Payment
from .services import (
    _request_payment,
    _verify_payment,
    process_gateway_callback,
    startpay,
)


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


@override_settings(
    PAYMENT_CALLBACK_BASE="https://backend.example/api/payment/callback/",
    PAYMENT_CURRENCY="IRT",
    ZARINPAL_MERCHANT_ID="merchant-id",
)
class IRTGatewayPayloadTests(TestCase):
    @patch("payment.services.requests.post")
    def test_request_sends_irt_and_verify_reuses_toman_amount(self, post):
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"data": {"code": 100}}

        _request_payment(
            merchant_id="merchant-id",
            amount=85_000,
            currency="IRT",
            description="presentation",
            email="buyer@example.com",
        )
        request_payload = post.call_args.kwargs["json"]

        _verify_payment(
            merchant_id="merchant-id",
            amount=85_000,
            authority="authority",
        )
        verify_payload = post.call_args.kwargs["json"]

        self.assertEqual(request_payload["amount"], 85_000)
        self.assertEqual(request_payload["currency"], "IRT")
        self.assertEqual(verify_payload["amount"], 85_000)
        self.assertNotIn("currency", verify_payload)


@override_settings(ZARINPAL_MERCHANT_ID="merchant-id")
class CoursePaymentLifecycleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="course-payer@example.com",
            password="password",
            is_email_verified=True,
        )
        self.course = Course.objects.create(
            name="One-seat presentation",
            offering_type=Course.OfferingType.ONLINE_PRESENTATION,
            capacity=1,
        )
        self.registration = Registration.objects.create(
            course=self.course,
            user=self.user,
            price=self.course.price,
            status=Registration.Status.APPROVED,
        )
        self.payment = Payment.objects.create(
            user=self.user,
            target_type=Payment.TargetType.COURSE,
            target_id=str(self.registration.id),
            amount=self.registration.price,
            currency="IRT",
            authority="COURSE-AUTHORITY",
            metadata={
                "reg_id": self.registration.id,
                "course_id": self.course.id,
            },
        )

    @patch("presentations.services.send_status_change_email")
    @patch("payment.services._verify_payment")
    def test_successful_payment_finalizes_reserved_seat(self, verify, _email):
        verify.return_value = {
            "data": {"code": 100, "ref_id": 123, "message": "Paid"}
        }

        process_gateway_callback(
            authority=self.payment.authority,
            gateway_status="OK",
        )

        self.registration.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.SUCCESSFUL)
        self.assertEqual(self.registration.status, Registration.Status.FINAL)
        self.assertEqual(self.course.remained_capacity(), 0)

    def test_failed_payment_releases_approved_seat(self):
        process_gateway_callback(
            authority=self.payment.authority,
            gateway_status="NOK",
        )

        self.registration.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.FAILED)
        self.assertEqual(self.registration.status, Registration.Status.CANCELLED)
        self.assertEqual(self.course.remained_capacity(), 1)

    @patch("presentations.services.send_status_change_email")
    @patch("presentations.services.initiate_payment_for_target")
    def test_retry_reclaims_capacity_before_issuing_link(self, initiate, _email):
        process_gateway_callback(
            authority=self.payment.authority,
            gateway_status="NOK",
        )
        initiate.return_value.url = "https://payment.example/retry"

        link = startpay(self.payment.authority)

        self.registration.refresh_from_db()
        self.assertEqual(link, "https://payment.example/retry")
        self.assertEqual(self.registration.status, Registration.Status.APPROVED)
        self.assertEqual(self.course.remained_capacity(), 0)
