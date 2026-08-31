from threading import Barrier, Lock, Thread
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import RequestFactory, TestCase, TransactionTestCase, skipUnlessDBFeature
from rest_framework.test import APIClient

from acm import error_codes as EC
from acm.exceptions import CustomAPIException
from .models import Course, Registration
from .admin import RegistrationAdmin
from .serializers import RegistrationSerializer
from .services import (
    initiate_registration_payment,
    promote_waitlist,
    set_status_approved,
    set_status_final,
    submit_registration,
)


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
                self.assertEqual(reg.payment_link, "")

                result = initiate_registration_payment(
                    registration_id=reg.id,
                    user=user,
                )

                self.assertEqual(result.url, "https://payment.example/start")
                call = self.initiate_payment.call_args_list[-1].kwargs
                self.assertEqual(call["amount"], price)
                self.assertEqual(call["target_id"], str(reg.id))
                self.assertEqual(call["extra_metadata"]["reg_id"], reg.id)

    def test_capacity_available_approves_without_starting_payment(self):
        course = self.make_course(
            Course.OfferingType.ONLINE_WORKSHOP,
            "legacy-approval",
            capacity=1,
            requires_approval=True,
        )

        reg = submit_registration(course=course, user=self.make_user("auto-pay"))

        self.assertEqual(reg.status, Registration.Status.APPROVED)
        self.assertEqual(reg.payment_link, "")
        self.initiate_payment.assert_not_called()

    def test_free_registration_finalizes_automatically(self):
        course = self.make_course(
            Course.OfferingType.ONLINE_WORKSHOP,
            "free",
            capacity=1,
            price=0,
            requires_approval=True,
        )

        reg = submit_registration(course=course, user=self.make_user("free"))

        self.assertEqual(reg.status, Registration.Status.FINAL)
        self.assertEqual(reg.payment_link, "")
        self.initiate_payment.assert_not_called()

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
        self.assertEqual(self.initiate_payment.call_count, 0)
        self.assertEqual(second.waitlist_position(), 1)

        with self.assertRaises(CustomAPIException) as raised:
            set_status_approved(second)
        self.assertEqual(raised.exception.app_code, EC.REG_CAPACITY_UNAVAILABLE)
        second.refresh_from_db()
        self.assertEqual(second.status, Registration.Status.RESERVED)

    def test_waitlist_positions_are_fifo_and_duplicate_submission_is_idempotent(self):
        course = self.make_course(
            Course.OfferingType.ONLINE_WORKSHOP,
            "fifo",
            capacity=1,
        )
        submit_registration(course=course, user=self.make_user("fifo-owner"))
        users = [self.make_user(f"fifo-{index}") for index in range(3)]
        waitlisted = [
            submit_registration(course=course, user=user) for user in users
        ]
        original_submitted_at = waitlisted[1].submitted_at

        duplicate = submit_registration(course=course, user=users[1])

        self.assertEqual(Registration.objects.filter(course=course).count(), 4)
        self.assertEqual(duplicate.id, waitlisted[1].id)
        self.assertEqual(duplicate.submitted_at, original_submitted_at)
        self.assertEqual(
            [reg.waitlist_position() for reg in waitlisted],
            [1, 2, 3],
        )
        self.assertEqual(
            RegistrationSerializer(waitlisted[1]).data["waitlist_position"],
            2,
        )

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

    def test_payment_initiation_failure_keeps_the_approved_seat_claim(self):
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

        user = self.make_user("rollback")
        reg = submit_registration(course=course, user=user)

        with self.assertRaises(CustomAPIException):
            initiate_registration_payment(registration_id=reg.id, user=user)

        reg.refresh_from_db()
        self.assertEqual(reg.status, Registration.Status.APPROVED)
        self.assertEqual(reg.payment_link, "")
        self.assertEqual(course.remained_capacity(), 0)

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

    def test_capacity_cannot_be_reduced_below_allocated_seats(self):
        course = self.make_course(
            Course.OfferingType.ONLINE_WORKSHOP,
            "capacity-floor",
            capacity=1,
        )
        submit_registration(course=course, user=self.make_user("capacity-floor"))
        course.capacity = 0

        with self.assertRaises(ValidationError):
            course.save(update_fields=["capacity"])

        course.refresh_from_db()
        self.assertEqual(course.capacity, 1)


class WaitlistPromotionTests(TestCase):
    def setUp(self):
        self.payment_patcher = patch(
            "presentations.services.initiate_payment_for_target",
            return_value=payment_result(),
        )
        self.status_email_patcher = patch(
            "presentations.services.send_status_change_email"
        )
        self.promotion_email_patcher = patch(
            "presentations.services.send_email_with_custom_template"
        )
        self.initiate_payment = self.payment_patcher.start()
        self.send_status_email = self.status_email_patcher.start()
        self.send_promotion_email = self.promotion_email_patcher.start()
        self.addCleanup(self.payment_patcher.stop)
        self.addCleanup(self.status_email_patcher.stop)
        self.addCleanup(self.promotion_email_patcher.stop)
        self.course = Course.objects.create(
            name="Promotion workshop",
            offering_type=Course.OfferingType.ONLINE_WORKSHOP,
            capacity=1,
            requires_approval=True,
        )

    def make_user(self, suffix: str):
        return get_user_model().objects.create_user(
            email=f"promotion-{suffix}@example.com",
            is_email_verified=True,
        )

    def fill_and_waitlist(self, waitlist_size: int = 3):
        owner = submit_registration(course=self.course, user=self.make_user("owner"))
        waitlisted = [
            submit_registration(
                course=self.course,
                user=self.make_user(f"waiting-{index}"),
            )
            for index in range(waitlist_size)
        ]
        return owner, waitlisted

    def test_capacity_increase_promotes_exact_fifo_count_and_emails(self):
        _owner, waitlisted = self.fill_and_waitlist()

        with self.captureOnCommitCallbacks(execute=True):
            self.course.capacity = 3
            self.course.save(update_fields=["capacity"])

        for reg in waitlisted:
            reg.refresh_from_db()
        self.assertEqual(
            [reg.status for reg in waitlisted],
            [
                Registration.Status.APPROVED,
                Registration.Status.APPROVED,
                Registration.Status.RESERVED,
            ],
        )
        self.assertEqual(waitlisted[2].waitlist_position(), 1)
        self.assertEqual(self.course.remained_capacity(), 0)
        self.assertEqual(self.send_promotion_email.call_count, 2)
        notified = [
            call.kwargs["to"]
            for call in self.send_promotion_email.call_args_list
        ]
        self.assertEqual(
            notified,
            [waitlisted[0].user.email, waitlisted[1].user.email],
        )
        self.assertTrue(
            all(
                call.kwargs["template"] == "course_waitlist_promoted"
                for call in self.send_promotion_email.call_args_list
            )
        )

    def test_promotion_email_runs_only_after_commit_and_is_not_duplicated(self):
        _owner, waitlisted = self.fill_and_waitlist(waitlist_size=1)
        Course.objects.filter(id=self.course.id).update(capacity=2)

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            first = promote_waitlist(course_id=self.course.id)
            second = promote_waitlist(course_id=self.course.id)
            self.send_promotion_email.assert_not_called()

        self.assertEqual([reg.id for reg in first], [waitlisted[0].id])
        self.assertEqual(second, [])
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.send_promotion_email.assert_called_once()
        self.assertEqual(
            self.send_promotion_email.call_args.kwargs["deduplication_key"],
            f"course-waitlist-promoted:{waitlisted[0].id}",
        )

    def test_email_failure_does_not_roll_back_promotion(self):
        _owner, waitlisted = self.fill_and_waitlist(waitlist_size=1)
        Course.objects.filter(id=self.course.id).update(capacity=2)
        self.send_promotion_email.side_effect = RuntimeError("mail unavailable")

        with self.captureOnCommitCallbacks(execute=True):
            promote_waitlist(course_id=self.course.id)

        waitlisted[0].refresh_from_db()
        self.assertEqual(waitlisted[0].status, Registration.Status.APPROVED)
        self.assertEqual(self.course.remained_capacity(), 0)


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
        self.assertFalse(offering["requires_approval"])
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

    def test_registration_api_returns_current_waitlist_position(self):
        full_course = Course.objects.create(
            name="Full API workshop",
            offering_type=Course.OfferingType.ONLINE_WORKSHOP,
            capacity=0,
            requires_approval=True,
        )

        response = self.client.post(
            "/api/presentations/register/",
            {"course_id": full_course.id},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Registration.Status.RESERVED)
        self.assertEqual(response.data["waitlist_position"], 1)
        self.assertEqual(response.data["payment_link"], "")

    @patch("presentations.services.initiate_payment_for_target")
    def test_payment_api_creates_link_only_when_user_asks(self, initiate):
        registration_response = self.client.post(
            "/api/presentations/register/",
            {"course_id": self.course.id},
            format="json",
        )
        registration_id = registration_response.data["id"]

        self.assertEqual(registration_response.status_code, 200)
        self.assertEqual(
            registration_response.data["status"],
            Registration.Status.APPROVED,
        )
        self.assertEqual(registration_response.data["payment_link"], "")
        initiate.assert_not_called()

        initiate.return_value = SimpleNamespace(
            url="https://payment.example/on-demand",
            authority="ON-DEMAND-AUTHORITY",
            payment=SimpleNamespace(
                id=17,
                amount=self.course.price,
                currency="IRT",
                status="PENDING",
            ),
        )
        payment_response = self.client.post(
            f"/api/presentations/me/registrations/{registration_id}/payment/",
            format="json",
        )

        self.assertEqual(payment_response.status_code, 201)
        self.assertEqual(
            payment_response.data["payment_link"],
            "https://payment.example/on-demand",
        )
        self.assertEqual(payment_response.data["authority"], "ON-DEMAND-AUTHORITY")
        initiate.assert_called_once()
        registration = Registration.objects.get(id=registration_id)
        self.assertEqual(registration.payment_link, payment_response.data["payment_link"])

    @patch("presentations.services.initiate_payment_for_target")
    def test_payment_api_does_not_expose_another_users_registration(self, initiate):
        other_user = get_user_model().objects.create_user(
            email="another-buyer@example.com",
            is_email_verified=True,
        )
        registration = Registration.objects.create(
            course=self.course,
            user=other_user,
            price=self.course.price,
            status=Registration.Status.APPROVED,
        )

        response = self.client.post(
            f"/api/presentations/me/registrations/{registration.id}/payment/",
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["errorCode"], EC.PAY_NOT_OWNED)
        initiate.assert_not_called()

    @patch("presentations.services.initiate_payment_for_target")
    def test_payment_api_rejects_waitlisted_registration(self, initiate):
        registration = Registration.objects.create(
            course=self.course,
            user=self.user,
            price=self.course.price,
            status=Registration.Status.RESERVED,
        )

        response = self.client.post(
            f"/api/presentations/me/registrations/{registration.id}/payment/",
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["errorCode"], EC.REG_PAYMENT_NOT_AVAILABLE)
        initiate.assert_not_called()


class RegistrationAdminTests(TestCase):
    def test_status_is_editable_in_registration_change_form(self):
        registration_admin = RegistrationAdmin(Registration, AdminSite())

        self.assertNotIn("status", registration_admin.get_readonly_fields(None))

    @patch("presentations.services.initiate_payment_for_target")
    def test_approve_action_approves_without_creating_payment_link(self, initiate):
        admin_user = get_user_model().objects.create_superuser(
            email="registration-admin@example.com",
            password="password",
        )
        buyer = get_user_model().objects.create_user(
            email="admin-approved-buyer@example.com",
            is_email_verified=True,
        )
        course = Course.objects.create(
            name="Admin-approved workshop",
            offering_type=Course.OfferingType.ONLINE_WORKSHOP,
            capacity=1,
        )
        registration = Registration.objects.create(
            course=course,
            user=buyer,
            price=course.price,
            status=Registration.Status.QUEUED,
        )
        request = RequestFactory().post("/api/admin/presentations/registration/")
        request.user = admin_user
        registration_admin = RegistrationAdmin(Registration, AdminSite())

        with patch.object(registration_admin, "message_user") as message_user:
            registration_admin.approve_selected(
                request,
                Registration.objects.filter(id=registration.id),
            )

        registration.refresh_from_db()
        self.assertEqual(registration.status, Registration.Status.APPROVED)
        self.assertEqual(registration.payment_link, "")
        initiate.assert_not_called()
        message_user.assert_called_once_with(request, "Approved 1 registration(s)")
        self.assertIn("approve_selected", registration_admin.actions)


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

    def test_concurrent_registrations_allocate_one_seat_and_one_waitlist_row(self):
        course = Course.objects.create(
            name="Concurrent registration workshop",
            offering_type=Course.OfferingType.ONLINE_WORKSHOP,
            capacity=1,
            requires_approval=True,
        )
        users = [
            get_user_model().objects.create_user(
                email=f"register-concurrent-{index}@example.com",
                is_email_verified=True,
            )
            for index in range(2)
        ]
        barrier = Barrier(2)
        result_lock = Lock()
        results = []

        def register(user_id):
            close_old_connections()
            try:
                user = get_user_model().objects.get(id=user_id)
                barrier.wait()
                result = submit_registration(course=course, user=user).status
            finally:
                close_old_connections()
            with result_lock:
                results.append(result)

        with patch(
            "presentations.services.initiate_payment_for_target",
            return_value=payment_result(),
        ), patch("presentations.services.send_status_change_email"), patch(
            "presentations.services.send_email_with_custom_template"
        ):
            threads = [Thread(target=register, args=(user.id,)) for user in users]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(results.count(Registration.Status.APPROVED), 1)
        self.assertEqual(results.count(Registration.Status.RESERVED), 1)
        self.assertEqual(Registration.objects.filter(course=course).count(), 2)
        self.assertEqual(course.remained_capacity(), 0)

    def test_concurrent_promotions_promote_only_the_available_fifo_user(self):
        course = Course.objects.create(
            name="Concurrent promotion workshop",
            offering_type=Course.OfferingType.ONLINE_WORKSHOP,
            capacity=1,
        )
        users = [
            get_user_model().objects.create_user(
                email=f"promote-concurrent-{index}@example.com",
                is_email_verified=True,
            )
            for index in range(3)
        ]
        Registration.objects.create(
            course=course,
            user=users[0],
            price=course.price,
            status=Registration.Status.APPROVED,
        )
        waitlisted = [
            Registration.objects.create(
                course=course,
                user=user,
                price=course.price,
                status=Registration.Status.RESERVED,
            )
            for user in users[1:]
        ]
        Course.objects.filter(id=course.id).update(capacity=2)
        barrier = Barrier(2)
        result_lock = Lock()
        promoted_ids = []

        def promote():
            close_old_connections()
            try:
                barrier.wait()
                result = promote_waitlist(course_id=course.id)
            finally:
                close_old_connections()
            with result_lock:
                promoted_ids.extend(reg.id for reg in result)

        with patch(
            "presentations.services.initiate_payment_for_target",
            return_value=payment_result(),
        ), patch("presentations.services.send_email_with_custom_template") as email:
            threads = [Thread(target=promote) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(promoted_ids, [waitlisted[0].id])
        self.assertEqual(email.call_count, 1)
        self.assertEqual(
            Registration.objects.filter(
                course=course,
                status__in=(Registration.Status.APPROVED, Registration.Status.FINAL),
            ).count(),
            2,
        )
        waitlisted[1].refresh_from_db()
        self.assertEqual(waitlisted[1].status, Registration.Status.RESERVED)
        self.assertEqual(waitlisted[1].waitlist_position(), 1)
        self.assertEqual(course.remained_capacity(), 0)

    def test_concurrent_registration_cannot_jump_a_waitlist_promotion(self):
        course = Course.objects.create(
            name="Promotion fairness workshop",
            offering_type=Course.OfferingType.ONLINE_WORKSHOP,
            capacity=0,
        )
        waiting_user = get_user_model().objects.create_user(
            email="already-waiting@example.com",
            is_email_verified=True,
        )
        newcomer = get_user_model().objects.create_user(
            email="newcomer@example.com",
            is_email_verified=True,
        )
        waiting = Registration.objects.create(
            course=course,
            user=waiting_user,
            price=course.price,
            status=Registration.Status.RESERVED,
        )
        Course.objects.filter(id=course.id).update(capacity=1)
        barrier = Barrier(2)

        def promote():
            close_old_connections()
            try:
                barrier.wait()
                promote_waitlist(course_id=course.id)
            finally:
                close_old_connections()

        def register():
            close_old_connections()
            try:
                barrier.wait()
                submit_registration(course=course, user=newcomer)
            finally:
                close_old_connections()

        with patch(
            "presentations.services.initiate_payment_for_target",
            return_value=payment_result(),
        ), patch("presentations.services.send_status_change_email"), patch(
            "presentations.services.send_email_with_custom_template"
        ):
            threads = [Thread(target=promote), Thread(target=register)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        waiting.refresh_from_db()
        newcomer_reg = Registration.objects.get(course=course, user=newcomer)
        self.assertEqual(waiting.status, Registration.Status.APPROVED)
        self.assertEqual(newcomer_reg.status, Registration.Status.RESERVED)
        self.assertEqual(newcomer_reg.waitlist_position(), 1)
        self.assertEqual(course.remained_capacity(), 0)

    def test_concurrent_capacity_decrease_and_registration_remain_consistent(self):
        course = Course.objects.create(
            name="Concurrent capacity workshop",
            offering_type=Course.OfferingType.ONLINE_WORKSHOP,
            capacity=1,
        )
        user = get_user_model().objects.create_user(
            email="capacity-race@example.com",
            is_email_verified=True,
        )
        barrier = Barrier(2)
        result_lock = Lock()
        results = []

        def decrease_capacity():
            close_old_connections()
            try:
                current = Course.objects.get(id=course.id)
                current.capacity = 0
                barrier.wait()
                try:
                    current.save(update_fields=["capacity"])
                    result = "capacity-decreased"
                except ValidationError:
                    result = "capacity-preserved"
            finally:
                close_old_connections()
            with result_lock:
                results.append(result)

        def register():
            close_old_connections()
            try:
                barrier.wait()
                result = submit_registration(course=course, user=user).status
            finally:
                close_old_connections()
            with result_lock:
                results.append(result)

        with patch(
            "presentations.services.initiate_payment_for_target",
            return_value=payment_result(),
        ), patch("presentations.services.send_status_change_email"), patch(
            "presentations.services.send_email_with_custom_template"
        ):
            threads = [Thread(target=decrease_capacity), Thread(target=register)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        course.refresh_from_db()
        registration = Registration.objects.get(course=course, user=user)
        allocated = Registration.objects.filter(
            course=course,
            status__in=(Registration.Status.APPROVED, Registration.Status.FINAL),
        ).count()
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertLessEqual(allocated, course.capacity)
        if course.capacity == 0:
            self.assertEqual(registration.status, Registration.Status.RESERVED)
            self.assertIn("capacity-decreased", results)
        else:
            self.assertEqual(registration.status, Registration.Status.APPROVED)
            self.assertIn("capacity-preserved", results)

    def test_payment_finalization_and_capacity_update_use_compatible_locks(self):
        course = Course.objects.create(
            name="Concurrent payment capacity workshop",
            offering_type=Course.OfferingType.ONLINE_WORKSHOP,
            capacity=1,
        )
        user = get_user_model().objects.create_user(
            email="payment-capacity-race@example.com",
            is_email_verified=True,
        )
        registration = Registration.objects.create(
            course=course,
            user=user,
            price=course.price,
            status=Registration.Status.APPROVED,
        )
        barrier = Barrier(2)
        results = []
        result_lock = Lock()

        def finalize_payment():
            close_old_connections()
            try:
                barrier.wait()
                set_status_final([Registration.objects.get(id=registration.id)])
                result = "finalized"
            finally:
                close_old_connections()
            with result_lock:
                results.append(result)

        def decrease_capacity():
            close_old_connections()
            try:
                current = Course.objects.get(id=course.id)
                current.capacity = 0
                barrier.wait()
                try:
                    current.save(update_fields=["capacity"])
                    result = "capacity-decreased"
                except ValidationError:
                    result = "capacity-preserved"
            finally:
                close_old_connections()
            with result_lock:
                results.append(result)

        with patch("presentations.services.send_status_change_email"):
            threads = [Thread(target=finalize_payment), Thread(target=decrease_capacity)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        registration.refresh_from_db()
        course.refresh_from_db()
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(registration.status, Registration.Status.FINAL)
        self.assertEqual(course.capacity, 1)
        self.assertEqual(results.count("finalized"), 1)
        self.assertEqual(results.count("capacity-preserved"), 1)
        self.assertEqual(course.remained_capacity(), 0)
