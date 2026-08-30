from threading import Barrier, Lock, Thread
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from rest_framework.test import APIClient

from acm import error_codes as EC
from acm.exceptions import CustomAPIException
from .models import Course, Registration
from .serializers import RegistrationSerializer
from .services import set_status_approved, set_status_final, submit_registration


OFFERING_CASES = (
    (Course.OfferingType.ONLINE_PRESENTATION, 250, 85_000, True, False),
    (Course.OfferingType.OFFLINE_PRESENTATION, None, 60_000, False, False),
    (Course.OfferingType.IN_PERSON_WORKSHOP, 125, 125_000, False, True),
    (Course.OfferingType.ONLINE_WORKSHOP, 80, 85_000, True, False),
)


def payment_result():
    return SimpleNamespace(
        url="https://payment.example/start",
        authority="authority",
        payment=None,
    )


class IndividualOfferingTests(TestCase):
    def setUp(self):
        self.payment_patcher = patch(
            "presentations.services.initiate_payment_for_target",
            return_value=payment_result(),
        )
        self.email_patcher = patch(
            "presentations.services.send_status_change_email"
        )
        self.initiate_payment = self.payment_patcher.start()
        self.send_email = self.email_patcher.start()
        self.addCleanup(self.payment_patcher.stop)
        self.addCleanup(self.email_patcher.stop)

    def make_user(self, suffix: str):
        return get_user_model().objects.create_user(
            email=f"buyer-{suffix}@example.com",
            password="password",
            is_email_verified=True,
        )

    def make_course(self, offering_type: str, suffix: str, **overrides):
        values = {
            "name": f"Offering {suffix}",
            "offering_type": offering_type,
            "requires_approval": False,
        }
        values.update(overrides)
        return Course.objects.create(**values)

    def test_type_defaults_match_the_2026_catalogue(self):
        for index, (kind, capacity, price, online, onsite) in enumerate(
            OFFERING_CASES
        ):
            with self.subTest(offering_type=kind):
                course = self.make_course(kind, f"defaults-{index}")
                self.assertEqual(course.capacity, capacity)
                self.assertEqual(course.price, price)
                self.assertEqual(course.online, online)
                self.assertEqual(course.onsite, onsite)
                self.assertEqual(course.is_unlimited, capacity is None)

    def test_each_offering_type_can_be_purchased_individually(self):
        for index, (kind, _capacity, price, _online, _onsite) in enumerate(
            OFFERING_CASES
        ):
            with self.subTest(offering_type=kind):
                course = self.make_course(kind, f"purchase-{index}")
                user = self.make_user(f"purchase-{index}")

                reg = submit_registration(course=course, user=user)

                self.assertEqual(reg.status, Registration.Status.APPROVED)
                self.assertEqual(reg.price, price)
                self.assertFalse(reg.items.exists())
                call = self.initiate_payment.call_args_list[-1].kwargs
                self.assertEqual(call["amount"], price)
                self.assertEqual(call["target_id"], str(reg.id))
                self.assertEqual(call["extra_metadata"]["reg_id"], reg.id)

    def test_approved_registration_reserves_last_finite_seat(self):
        course = self.make_course(
            Course.OfferingType.ONLINE_WORKSHOP,
            "finite",
            capacity=1,
        )
        first = submit_registration(course=course, user=self.make_user("first"))
        second = submit_registration(course=course, user=self.make_user("second"))

        self.assertEqual(first.status, Registration.Status.APPROVED)
        self.assertEqual(second.status, Registration.Status.RESERVED)
        self.assertEqual(course.remained_capacity(), 0)
        self.assertEqual(self.initiate_payment.call_count, 1)

        with self.assertRaises(CustomAPIException) as raised:
            set_status_approved(second)
        self.assertEqual(raised.exception.app_code, EC.REG_CAPACITY_UNAVAILABLE)
        second.refresh_from_db()
        self.assertEqual(second.status, Registration.Status.RESERVED)

    def test_offline_presentations_have_unlimited_capacity(self):
        course = self.make_course(
            Course.OfferingType.OFFLINE_PRESENTATION,
            "unlimited",
        )

        registrations = [
            submit_registration(course=course, user=self.make_user(f"offline-{i}"))
            for i in range(4)
        ]

        self.assertTrue(
            all(reg.status == Registration.Status.APPROVED for reg in registrations)
        )
        self.assertIsNone(course.remained_capacity())

    def test_package_selection_is_rejected_without_consuming_capacity(self):
        parent = self.make_course(
            Course.OfferingType.ONLINE_PRESENTATION,
            "legacy-parent",
            capacity=1,
        )
        child = self.make_course(
            Course.OfferingType.ONLINE_WORKSHOP,
            "legacy-child",
            capacity=1,
        )
        parent.children.add(child)

        with self.assertRaises(CustomAPIException) as raised:
            submit_registration(
                course=parent,
                user=self.make_user("package"),
                child_ids=[child.id],
            )

        self.assertEqual(raised.exception.app_code, EC.REG_PACKAGE_UNAVAILABLE)
        self.assertEqual(Registration.objects.count(), 0)
        self.assertEqual(parent.remained_capacity(), 1)
        self.assertEqual(child.remained_capacity(), 1)

    def test_payment_initiation_failure_rolls_back_the_seat_claim(self):
        course = self.make_course(
            Course.OfferingType.IN_PERSON_WORKSHOP,
            "rollback",
            capacity=1,
        )
        self.initiate_payment.side_effect = CustomAPIException(
            code=EC.PAY_INIT_FAILED,
            message="gateway unavailable",
            status_code=409,
        )

        with self.assertRaises(CustomAPIException):
            submit_registration(course=course, user=self.make_user("rollback"))

        self.assertEqual(Registration.objects.count(), 0)
        self.assertEqual(course.remained_capacity(), 1)

    def test_registration_uses_price_snapshot(self):
        course = self.make_course(
            Course.OfferingType.ONLINE_PRESENTATION,
            "snapshot",
        )
        reg = submit_registration(course=course, user=self.make_user("snapshot"))
        course.price = 99_000
        course.save()

        self.assertEqual(RegistrationSerializer(reg).data["total_amount"], 85_000)

    def test_finalizing_an_approved_registration_does_not_consume_twice(self):
        course = self.make_course(
            Course.OfferingType.ONLINE_WORKSHOP,
            "final",
            capacity=1,
        )
        reg = submit_registration(course=course, user=self.make_user("final"))

        set_status_final([reg])

        reg.refresh_from_db()
        self.assertEqual(reg.status, Registration.Status.FINAL)
        self.assertEqual(course.remained_capacity(), 0)


class OfferingAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            email="api-buyer@example.com",
            password="password",
            is_email_verified=True,
        )
        self.client.force_authenticate(self.user)
        self.course = Course.objects.create(
            name="Offline API presentation",
            offering_type=Course.OfferingType.OFFLINE_PRESENTATION,
            requires_approval=True,
        )
        self.legacy = Course.objects.create(
            name="Legacy package",
            price=720_000,
            capacity=0,
        )

    def test_offerings_endpoint_exposes_type_capacity_and_irt(self):
        response = self.client.get("/api/presentations/offerings/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        offering = response.data[0]
        self.assertEqual(offering["offering_type"], "OFFLINE_PRESENTATION")
        self.assertIsNone(offering["capacity"])
        self.assertIsNone(offering["remained_capacity"])
        self.assertTrue(offering["is_unlimited"])
        self.assertEqual(offering["price"], 60_000)
        self.assertEqual(offering["currency"], "IRT")
        self.assertNotIn("children", offering)

    def test_registration_api_rejects_legacy_child_ids(self):
        response = self.client.post(
            "/api/presentations/register/",
            {"course_id": self.course.id, "child_ids": [self.legacy.id]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("child_ids", response.data["errorMessage"])
        self.assertFalse(Registration.objects.exists())


@skipUnlessDBFeature("has_select_for_update")
class CapacityConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_concurrent_approvals_cannot_oversell(self):
        course = Course.objects.create(
            name="Concurrent workshop",
            offering_type=Course.OfferingType.ONLINE_WORKSHOP,
            capacity=1,
            requires_approval=True,
        )
        users = [
            get_user_model().objects.create_user(
                email=f"concurrent-{i}@example.com",
                is_email_verified=True,
            )
            for i in range(2)
        ]
        registrations = [
            Registration.objects.create(
                course=course,
                user=user,
                price=course.price,
                status=Registration.Status.QUEUED,
            )
            for user in users
        ]
        barrier = Barrier(2)
        result_lock = Lock()
        results = []

        def approve(registration_id):
            close_old_connections()
            try:
                barrier.wait()
                set_status_approved(Registration.objects.get(id=registration_id))
                result = "approved"
            except CustomAPIException as exc:
                result = exc.app_code
            finally:
                close_old_connections()
            with result_lock:
                results.append(result)

        with patch(
            "presentations.services.initiate_payment_for_target",
            return_value=payment_result(),
        ), patch("presentations.services.send_status_change_email"):
            threads = [
                Thread(target=approve, args=(registration.id,))
                for registration in registrations
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(results.count("approved"), 1)
        self.assertEqual(results.count(EC.REG_CAPACITY_UNAVAILABLE), 1)
        self.assertEqual(
            Registration.objects.filter(status=Registration.Status.APPROVED).count(),
            1,
        )
