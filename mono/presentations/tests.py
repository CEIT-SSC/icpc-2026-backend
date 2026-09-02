import importlib
from datetime import time, timedelta
from threading import Barrier, Lock, Thread
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import (
    RequestFactory,
    TestCase,
    TransactionTestCase,
    skipUnlessDBFeature,
)
from django.utils import timezone
from rest_framework.test import APIClient

from acm import error_codes as EC
from acm.exceptions import CustomAPIException
from payment.models import Payment

from .admin import CourseAdminForm, RegistrationAdmin
from .models import (
    BUNDLE_CATALOG,
    Course,
    CourseSession,
    DiscountCode,
    Registration,
    RegistrationItem,
    ScheduleRule,
)
from .serializers import RegistrationSerializer
from .services import (
    _user_has_access_to_course,
    cancel_registration_for_failed_payment,
    create_skyroom_link,
    get_course_sessions,
    initiate_registration_payment,
    promote_waitlist,
    set_status_approved,
    set_status_final,
    submit_registration,
)


def gateway_result(amount=599_000):
    return SimpleNamespace(
        url="https://payment.example/start",
        authority="authority",
        payment=SimpleNamespace(
            id=17,
            amount=amount,
            currency="IRT",
            status=Payment.Status.PENDING,
        ),
    )


class BundleTestMixin:
    def setUp(self):
        super().setUp()
        self.status_email_patcher = patch(
            "presentations.services.send_status_change_email"
        )
        self.promotion_email_patcher = patch(
            "presentations.services.send_email_with_custom_template"
        )
        self.status_email_patcher.start()
        self.promotion_email_patcher.start()
        self.addCleanup(self.status_email_patcher.stop)
        self.addCleanup(self.promotion_email_patcher.stop)

    def make_user(self, suffix: str, *, verified=True):
        return get_user_model().objects.create_user(
            email=f"buyer-{suffix}@example.com",
            password="password",
            is_email_verified=verified,
        )

    def make_bundle(
        self,
        bundle_type=Course.BundleType.ALL_ONLINE_PRESENTATIONS,
        *,
        capacities=None,
        price=None,
        slug_suffix="",
        schedules=True,
    ):
        config = BUNDLE_CATALOG[bundle_type]
        count = config["expected_member_count"]
        capacities = capacities or [10] * count
        members = []
        for index in range(count):
            member = Course.objects.create(
                name=f"{bundle_type} member {slug_suffix}{index}",
                slug=f"{bundle_type.lower()}-{slug_suffix}{index}",
                offering_type=config["offering_type"],
                capacity=capacities[index] if capacities[index] is not None else 1,
            )
            if capacities[index] is None:
                Course.objects.filter(pk=member.pk).update(capacity=None)
                member.refresh_from_db()
            members.append(member)
        bundle = Course.objects.create(
            name=config["name"],
            slug=f"{config['slug']}{slug_suffix}",
            bundle_type=bundle_type,
            price=config["price"] if price is None else price,
            capacity=None,
        )
        bundle.children.add(*members)
        if schedules:
            rules = (
                ((6, time(17), time(20)), (1, time(17), time(20)))
                if bundle_type == Course.BundleType.ALL_ONLINE_PRESENTATIONS
                else ((3, time(9), time(12)),)
            )
            for target in [bundle, *members]:
                for weekday, start, end in rules:
                    ScheduleRule.objects.create(
                        course=target,
                        weekday=weekday,
                        start_time=start,
                        end_time=end,
                    )
        return bundle, members


class BundleCatalogueAPITests(BundleTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.bundles = [
            self.make_bundle(
                Course.BundleType.ALL_ONLINE_PRESENTATIONS,
                capacities=[250] * 6,
            )[0],
            self.make_bundle(
                Course.BundleType.ALL_IN_PERSON_WORKSHOPS,
                capacities=[125] * 3,
            )[0],
            self.make_bundle(
                Course.BundleType.ALL_ONLINE_WORKSHOPS,
                capacities=[80] * 3,
            )[0],
        ]
        Course.objects.create(
            name="Legacy active individual",
            slug="legacy-active-individual",
            offering_type=Course.OfferingType.OFFLINE_PRESENTATION,
        )
        Course.objects.create(
            name="Invalid empty typed bundle",
            slug="invalid-empty-bundle",
            bundle_type=Course.BundleType.ALL_ONLINE_WORKSHOPS,
            price=1,
        )

    def test_only_three_valid_bundles_are_public_with_explicit_contract(self):
        response = self.client.get("/api/presentations/offerings/")

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
        self.assertEqual(
            {row["bundle_type"] for row in response.data},
            set(Course.BundleType.values),
        )
        expected_prices = {
            key: config["price"] for key, config in BUNDLE_CATALOG.items()
        }
        expected_counts = {
            key: config["expected_member_count"]
            for key, config in BUNDLE_CATALOG.items()
        }
        for row in response.data:
            config = BUNDLE_CATALOG[row["bundle_type"]]
            self.assertEqual(row["price"], expected_prices[row["bundle_type"]])
            self.assertEqual(row["member_count"], expected_counts[row["bundle_type"]])
            self.assertEqual(len(row["members"]), row["member_count"])
            self.assertEqual(row["category"], config["category"])
            self.assertEqual(row["delivery_mode"], config["delivery_mode"])
            self.assertEqual(row["online"], config["online"])
            self.assertEqual(row["onsite"], config["onsite"])
            self.assertEqual(row["currency"], "IRT")
            self.assertFalse(row["requires_approval"])
            self.assertTrue(row["is_active"])
            self.assertTrue(all("presenters" in member for member in row["members"]))

    def test_bundle_schedules_and_bottleneck_capacity_are_serialized(self):
        presentation = self.bundles[0]
        first_member = presentation.children.order_by("id").first()
        first_member.capacity = 7
        first_member.save(update_fields=["capacity"])

        response = self.client.get(f"/api/presentations/course/{presentation.slug}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["capacity"], 7)
        self.assertEqual(response.data["remained_capacity"], 7)
        self.assertFalse(response.data["is_unlimited"])
        self.assertEqual(
            {(rule["weekday"], rule["start_time"], rule["end_time"]) for rule in response.data["schedule"]},
            {(1, "17:00:00", "20:00:00"), (6, "17:00:00", "20:00:00")},
        )
        self.assertTrue(
            all(len(member["schedule"]) == 2 for member in response.data["members"])
        )


class BundleRegistrationTests(BundleTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("registration")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.bundle, self.members = self.make_bundle(capacities=[2] * 6)

    def test_direct_component_and_every_child_ids_payload_are_rejected(self):
        direct = self.client.post(
            "/api/presentations/register/",
            {"course_id": self.members[0].id},
            format="json",
        )
        self.assertEqual(direct.status_code, 409)
        self.assertEqual(direct.data["errorCode"], EC.REG_OFFERING_UNAVAILABLE)

        for child_ids in ([], [self.members[0].id]):
            with self.subTest(child_ids=child_ids):
                response = self.client.post(
                    "/api/presentations/register/",
                    {"course_id": self.bundle.id, "child_ids": child_ids},
                    format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["errorCode"], EC.REG_PACKAGE_UNAVAILABLE)
                self.assertIn("child_ids", response.data["errorMessage"])
        self.assertFalse(Registration.objects.exists())

    def test_server_snapshots_every_member_at_zero_and_parent_price(self):
        response = self.client.post(
            "/api/presentations/register/",
            {"course_id": self.bundle.id, "extra_answers": {}},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Registration.Status.APPROVED)
        self.assertEqual(response.data["price"], 599_000)
        self.assertEqual(response.data["total_amount"], 599_000)
        self.assertEqual(len(response.data["items"]), 6)
        self.assertEqual({item["price"] for item in response.data["items"]}, {0})
        self.assertEqual(
            {item["child"]["id"] for item in response.data["items"]},
            {member.id for member in self.members},
        )
        self.assertEqual(response.data["course"]["bundle_type"], self.bundle.bundle_type)
        self.assertEqual(response.data["course"]["category"], "PRESENTATION")
        self.assertEqual(response.data["course"]["delivery_mode"], "ONLINE")
        self.assertEqual(response.data["payment_link"], "")

        self.bundle.price = 1
        self.bundle.save(update_fields=["price"])
        reg = Registration.objects.prefetch_related("items").get()
        self.assertEqual(RegistrationSerializer(reg).data["total_amount"], 599_000)

    def test_legacy_member_ownership_deduplicates_capacity_and_does_not_block_bundle(self):
        member = self.members[0]
        member.capacity = 1
        member.save(update_fields=["capacity"])
        Registration.objects.create(
            course=member,
            user=self.user,
            price=member.price,
            status=Registration.Status.FINAL,
        )

        own_bundle = submit_registration(course=self.bundle, user=self.user)
        other_bundle = submit_registration(
            course=self.bundle,
            user=self.make_user("distinct-other"),
        )

        self.assertEqual(own_bundle.status, Registration.Status.APPROVED)
        self.assertEqual(other_bundle.status, Registration.Status.RESERVED)
        self.assertEqual(member.remained_capacity(), 0)

    def test_waitlist_resubmission_preserves_fifo_price_composition_and_timestamp(self):
        for member in self.members:
            Course.objects.filter(pk=member.pk).update(capacity=0)
        reg = submit_registration(course=self.bundle, user=self.user)
        item_ids = list(reg.items.values_list("id", flat=True))
        submitted_at = reg.submitted_at
        original_price = reg.price
        self.bundle.price = 123
        self.bundle.save(update_fields=["price"])

        duplicate = submit_registration(course=self.bundle, user=self.user)

        self.assertEqual(duplicate.id, reg.id)
        self.assertEqual(duplicate.submitted_at, submitted_at)
        self.assertEqual(duplicate.price, original_price)
        self.assertEqual(list(duplicate.items.values_list("id", flat=True)), item_ids)
        self.assertEqual(duplicate.waitlist_position(), 1)

    def test_verified_email_is_still_required(self):
        with self.assertRaises(CustomAPIException) as raised:
            submit_registration(
                course=self.bundle,
                user=self.make_user("unverified", verified=False),
            )
        self.assertEqual(raised.exception.app_code, EC.ACC_EMAIL_NOT_VERIFIED)


class DiscountCodeTests(BundleTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.bundle, self.members = self.make_bundle(capacities=[2] * 6)
        self.user = self.make_user("discount")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_registration_reserves_discount_and_payment_uses_snapshot(self):
        discount = DiscountCode.objects.create(
            code="launch25",
            percent_off=25,
            max_uses=2,
        )

        response = self.client.post(
            "/api/presentations/register/",
            {"course_id": self.bundle.id, "discount_code": " launch25 "},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["discount_code"], "LAUNCH25")
        self.assertEqual(response.data["price"], 449_250)
        self.assertEqual(response.data["total_amount"], 449_250)
        discount.refresh_from_db()
        self.assertEqual(discount.used_count, 1)

        reg = Registration.objects.get(pk=response.data["id"])
        with patch(
            "presentations.services.initiate_payment_for_target",
            return_value=gateway_result(449_250),
        ) as initiate:
            initiate_registration_payment(registration_id=reg.id, user=self.user)
        self.assertEqual(initiate.call_args.kwargs["amount"], 449_250)
        self.assertEqual(
            initiate.call_args.kwargs["extra_metadata"]["discount_code"],
            "LAUNCH25",
        )

    def test_zero_price_discount_finalizes_without_payment(self):
        discount = DiscountCode.objects.create(code="free", amount_off=1_000_000)

        reg = submit_registration(
            course=self.bundle,
            user=self.user,
            discount_code=discount.code,
        )

        self.assertEqual(reg.price, 0)
        self.assertEqual(reg.status, Registration.Status.FINAL)
        with self.assertRaises(CustomAPIException) as raised:
            initiate_registration_payment(registration_id=reg.id, user=self.user)
        self.assertEqual(raised.exception.app_code, EC.REG_PAYMENT_NOT_AVAILABLE)

    def test_waitlist_retry_preserves_snapshot_without_double_redemption(self):
        for member in self.members:
            Course.objects.filter(pk=member.pk).update(capacity=0)
        discount = DiscountCode.objects.create(code="wait", amount_off=99_000)

        first = submit_registration(
            course=self.bundle,
            user=self.user,
            discount_code="WAIT",
        )
        second = submit_registration(
            course=self.bundle,
            user=self.user,
            discount_code="WAIT",
        )

        discount.refresh_from_db()
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.price, 500_000)
        self.assertEqual(discount.used_count, 1)

    def test_usage_limit_and_per_user_reuse_are_rejected(self):
        discount = DiscountCode.objects.create(
            code="once",
            amount_off=10_000,
            max_uses=2,
        )
        submit_registration(
            course=self.bundle,
            user=self.user,
            discount_code=discount.code,
        )
        other_bundle, _ = self.make_bundle(
            Course.BundleType.ALL_ONLINE_WORKSHOPS,
        )

        with self.assertRaises(CustomAPIException) as reused:
            submit_registration(
                course=other_bundle,
                user=self.user,
                discount_code=discount.code,
            )
        self.assertEqual(reused.exception.app_code, EC.DISCOUNT_ALREADY_USED)

        submit_registration(
            course=other_bundle,
            user=self.make_user("discount-second"),
            discount_code=discount.code,
        )
        with self.assertRaises(CustomAPIException) as exhausted:
            submit_registration(
                course=other_bundle,
                user=self.make_user("discount-third"),
                discount_code=discount.code,
            )
        self.assertEqual(exhausted.exception.app_code, EC.DISCOUNT_LIMIT_REACHED)

    def test_validation_endpoint_does_not_consume_code(self):
        discount = DiscountCode.objects.create(
            code="preview",
            percent_off=10,
            valid_from=timezone.now() - timedelta(hours=1),
            valid_until=timezone.now() + timedelta(hours=1),
        )

        response = self.client.post(
            "/api/presentations/discount/validate/",
            {"course_id": self.bundle.id, "code": "preview"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "PREVIEW")
        self.assertEqual(response.data["original_price"], 599_000)
        self.assertEqual(response.data["final_price"], 539_100)
        discount.refresh_from_db()
        self.assertEqual(discount.used_count, 0)

    def test_expired_and_wrong_bundle_codes_return_standard_errors(self):
        expired = DiscountCode.objects.create(
            code="expired",
            amount_off=10,
            valid_until=timezone.now() - timedelta(seconds=1),
        )
        wrong_bundle, _ = self.make_bundle(
            Course.BundleType.ALL_IN_PERSON_WORKSHOPS,
        )
        targeted = DiscountCode.objects.create(
            code="targeted",
            amount_off=10,
            course=wrong_bundle,
        )

        for code, expected_error in (
            (expired.code, EC.DISCOUNT_EXPIRED),
            (targeted.code, EC.DISCOUNT_NOT_APPLICABLE),
        ):
            response = self.client.post(
                "/api/presentations/discount/validate/",
                {"course_id": self.bundle.id, "code": code},
                format="json",
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["errorCode"], expected_error)

    def test_model_rejects_ambiguous_discount_value(self):
        discount = DiscountCode(
            code="ambiguous",
            percent_off=10,
            amount_off=10,
        )
        with self.assertRaises(ValidationError):
            discount.full_clean()

    def test_deleting_registration_releases_discount_reservation(self):
        discount = DiscountCode.objects.create(code="released", amount_off=10)
        registration = submit_registration(
            course=self.bundle,
            user=self.user,
            discount_code=discount.code,
        )

        registration.delete()

        discount.refresh_from_db()
        self.assertEqual(discount.used_count, 0)


class BundleCapacityAndPromotionTests(BundleTestMixin, TestCase):
    def test_minimum_zero_and_all_unlimited_capacity(self):
        bottleneck, members = self.make_bundle(
            Course.BundleType.ALL_ONLINE_WORKSHOPS,
            capacities=[5, 2, 7],
        )
        Registration.objects.create(
            course=members[1],
            user=self.make_user("bottleneck"),
            price=members[1].price,
            status=Registration.Status.APPROVED,
        )
        self.assertEqual(bottleneck.effective_capacity(), 2)
        self.assertEqual(bottleneck.remained_capacity(), 1)

        Course.objects.filter(pk=members[1].pk).update(capacity=1)
        members[1].refresh_from_db()
        self.assertEqual(bottleneck.remained_capacity(), 0)

        unlimited, _ = self.make_bundle(
            Course.BundleType.ALL_IN_PERSON_WORKSHOPS,
            capacities=[None, None, None],
            slug_suffix="-unlimited",
        )
        self.assertIsNone(unlimited.effective_capacity())
        self.assertIsNone(unlimited.remained_capacity())
        self.assertTrue(unlimited.is_unlimited)

    def test_bundle_fifo_promotion_does_not_skip_an_older_row(self):
        bundle, members = self.make_bundle(
            Course.BundleType.ALL_ONLINE_WORKSHOPS,
            capacities=[1, 2, 2],
        )
        overlapping_user = self.make_user("already-in-bottleneck")
        Registration.objects.create(
            course=members[0],
            user=overlapping_user,
            price=members[0].price,
            status=Registration.Status.APPROVED,
        )
        older = submit_registration(course=bundle, user=self.make_user("fifo-older"))
        newer = submit_registration(course=bundle, user=overlapping_user)

        promoted = promote_waitlist(course_id=members[1].id)

        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual(promoted, [])
        self.assertEqual(older.status, Registration.Status.RESERVED)
        self.assertEqual(newer.status, Registration.Status.RESERVED)
        self.assertEqual([older.waitlist_position(), newer.waitlist_position()], [1, 2])

    def test_released_direct_member_seat_promotes_affected_bundle_queue(self):
        bundle, members = self.make_bundle(
            Course.BundleType.ALL_ONLINE_WORKSHOPS,
            capacities=[1, 2, 2],
        )
        direct_user = self.make_user("legacy-direct-owner")
        direct = Registration.objects.create(
            course=members[0],
            user=direct_user,
            price=members[0].price,
            status=Registration.Status.APPROVED,
        )
        waiting = submit_registration(course=bundle, user=self.make_user("waiting"))
        self.assertEqual(waiting.status, Registration.Status.RESERVED)

        with self.captureOnCommitCallbacks(execute=True):
            cancel_registration_for_failed_payment(direct.id, user=direct_user)

        waiting.refresh_from_db()
        self.assertEqual(waiting.status, Registration.Status.APPROVED)
        self.assertEqual(waiting.payment_link, "")

    def test_capacity_increase_on_any_member_promotes_bundle_fifo(self):
        bundle, members = self.make_bundle(
            Course.BundleType.ALL_ONLINE_WORKSHOPS,
            capacities=[0, 2, 2],
        )
        waiting = submit_registration(course=bundle, user=self.make_user("increase"))

        with self.captureOnCommitCallbacks(execute=True):
            members[0].capacity = 1
            members[0].save(update_fields=["capacity"])

        waiting.refresh_from_db()
        self.assertEqual(waiting.status, Registration.Status.APPROVED)


class BundlePaymentAndAccessTests(BundleTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.bundle, self.members = self.make_bundle(capacities=[1] * 6)
        self.user = self.make_user("payment")
        self.reg = submit_registration(course=self.bundle, user=self.user)

    @patch("presentations.services.initiate_payment_for_target")
    def test_payment_starts_on_demand_using_snapshot_and_success_finalizes_all(self, initiate):
        initiate.return_value = gateway_result(self.reg.price)

        result = initiate_registration_payment(
            registration_id=self.reg.id,
            user=self.user,
        )
        self.assertEqual(result.url, "https://payment.example/start")
        self.assertEqual(initiate.call_args.kwargs["amount"], 599_000)
        self.assertEqual(initiate.call_args.kwargs["target_id"], str(self.reg.id))
        self.assertFalse(
            any(_user_has_access_to_course(self.user, member) for member in self.members)
        )

        set_status_final([self.reg])
        self.assertTrue(
            all(_user_has_access_to_course(self.user, member) for member in self.members)
        )

    @patch("presentations.services.initiate_payment_for_target")
    def test_failure_releases_every_claim_and_cancelled_retry_reclaims_atomically(self, initiate):
        cancel_registration_for_failed_payment(self.reg.id, user=self.user)
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.status, Registration.Status.CANCELLED)
        self.assertTrue(all(member.remained_capacity() == 1 for member in self.members))

        initiate.return_value = gateway_result(self.reg.price)
        initiate_registration_payment(registration_id=self.reg.id, user=self.user)
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.status, Registration.Status.APPROVED)
        self.assertTrue(all(member.remained_capacity() == 0 for member in self.members))

    def test_bundle_payment_failure_promotes_the_oldest_bundle_waiter(self):
        waiting = submit_registration(
            course=self.bundle,
            user=self.make_user("bundle-failure-waiter"),
        )
        self.assertEqual(waiting.status, Registration.Status.RESERVED)

        with self.captureOnCommitCallbacks(execute=True):
            cancel_registration_for_failed_payment(self.reg.id, user=self.user)

        self.reg.refresh_from_db()
        waiting.refresh_from_db()
        self.assertEqual(self.reg.status, Registration.Status.CANCELLED)
        self.assertEqual(waiting.status, Registration.Status.APPROVED)
        self.assertTrue(all(member.remained_capacity() == 0 for member in self.members))

    @patch("presentations.services.initiate_payment_for_target")
    def test_cancelled_bundle_retry_cannot_jump_a_relevant_legacy_waitlist(self, initiate):
        cancel_registration_for_failed_payment(self.reg.id, user=self.user)
        Registration.objects.create(
            course=self.members[0],
            user=self.make_user("legacy-waiter-priority"),
            price=self.members[0].price,
            status=Registration.Status.RESERVED,
        )

        with self.assertRaises(CustomAPIException) as raised:
            initiate_registration_payment(registration_id=self.reg.id, user=self.user)

        self.assertEqual(raised.exception.app_code, EC.REG_CAPACITY_UNAVAILABLE)
        initiate.assert_not_called()

    def test_typed_access_uses_item_snapshot_after_membership_changes(self):
        set_status_final([self.reg])
        original_member = self.members[0]
        self.bundle.children.remove(original_member)

        self.assertTrue(_user_has_access_to_course(self.user, original_member))

    def test_all_member_session_and_skyroom_access_starts_only_after_finalization(self):
        for index, member in enumerate(self.members):
            CourseSession.objects.create(
                course=member,
                title=f"Session {index}",
                date="2026-09-01",
                start_time=time(17),
                end_time=time(20),
            )
        self.assertTrue(
            all(get_course_sessions(self.user, member) is None for member in self.members)
        )
        with patch("presentations.services._now_in_shift_window", return_value=True), patch(
            "presentations.services.get_skyroom_presentation_link",
            return_value="https://skyroom.example/login",
        ):
            self.assertIsNone(create_skyroom_link(self.user, self.members[0]))

            set_status_final([self.reg])

            self.assertTrue(
                all(get_course_sessions(self.user, member).count() == 1 for member in self.members)
            )
            self.assertEqual(
                create_skyroom_link(self.user, self.members[0]),
                "https://skyroom.example/login",
            )

    def test_legacy_direct_and_parent_relationship_access_still_work(self):
        legacy_user = self.make_user("legacy-access")
        direct = self.members[0]
        legacy_parent = Course.objects.create(
            name="Legacy package",
            slug="legacy-package-access",
            price=10,
            capacity=None,
        )
        legacy_parent.children.add(direct)
        Registration.objects.create(
            course=legacy_parent,
            user=legacy_user,
            price=10,
            status=Registration.Status.FINAL,
        )
        self.assertTrue(_user_has_access_to_course(legacy_user, direct))


class LegacyLifecycleAndHistoryTests(BundleTestMixin, TestCase):
    def test_hidden_approved_individual_remains_payable_and_finalizable(self):
        component = Course.objects.create(
            name="Historical component",
            slug="historical-component",
            offering_type=Course.OfferingType.ONLINE_WORKSHOP,
            capacity=1,
        )
        user = self.make_user("legacy-pay")
        reg = Registration.objects.create(
            course=component,
            user=user,
            price=77_000,
            status=Registration.Status.APPROVED,
        )
        with patch(
            "presentations.services.initiate_payment_for_target",
            return_value=gateway_result(77_000),
        ) as initiate:
            initiate_registration_payment(registration_id=reg.id, user=user)
        self.assertEqual(initiate.call_args.kwargs["amount"], 77_000)
        set_status_final([reg])
        reg.refresh_from_db()
        self.assertEqual(reg.status, Registration.Status.FINAL)

    def test_history_serializes_legacy_and_bundle_rows_truthfully(self):
        bundle, _ = self.make_bundle()
        user = self.make_user("history")
        bundle_reg = submit_registration(course=bundle, user=user)
        legacy = Course.objects.create(
            name="Old individual",
            slug="old-individual",
            price=42,
            capacity=None,
        )
        legacy_reg = Registration.objects.create(
            course=legacy,
            user=user,
            price=41,
            status=Registration.Status.CANCELLED,
        )
        client = APIClient()
        client.force_authenticate(user)

        response = client.get("/api/presentations/me/registrations/")

        self.assertEqual(response.status_code, 200)
        rows = {row["id"]: row for row in response.data}
        self.assertEqual(rows[bundle_reg.id]["course"]["bundle_type"], bundle.bundle_type)
        self.assertEqual(len(rows[bundle_reg.id]["items"]), 6)
        self.assertIsNone(rows[legacy_reg.id]["course"]["bundle_type"])
        self.assertEqual(rows[legacy_reg.id]["total_amount"], 41)


class BundleMigrationTests(TestCase):
    def make_component(self, offering_type, index):
        return Course.objects.create(
            name=f"Migration {offering_type} {index}",
            slug=f"migration-{offering_type.lower()}-{index}",
            offering_type=offering_type,
        )

    def test_data_migration_is_idempotent_and_preserves_all_legacy_rows(self):
        groups = {
            Course.OfferingType.ONLINE_PRESENTATION: [
                self.make_component(Course.OfferingType.ONLINE_PRESENTATION, i)
                for i in range(6)
            ],
            Course.OfferingType.IN_PERSON_WORKSHOP: [
                self.make_component(Course.OfferingType.IN_PERSON_WORKSHOP, i)
                for i in range(3)
            ],
            Course.OfferingType.ONLINE_WORKSHOP: [
                self.make_component(Course.OfferingType.ONLINE_WORKSHOP, i)
                for i in range(3)
            ],
        }
        user = get_user_model().objects.create_user(email="migration@example.com")
        legacy_parent = Course.objects.create(
            name="Migration legacy parent",
            slug="migration-legacy-parent",
            price=123,
            capacity=None,
        )
        legacy_parent.children.add(groups[Course.OfferingType.ONLINE_PRESENTATION][0])
        reactivated_component = groups[Course.OfferingType.ONLINE_PRESENTATION][0]
        original_component_updated_at = reactivated_component.updated_at
        Course.objects.filter(pk=reactivated_component.pk).update(is_active=False)
        reg = Registration.objects.create(
            course=legacy_parent,
            user=user,
            price=111,
            status=Registration.Status.FINAL,
        )
        item = RegistrationItem.objects.create(
            registration=reg,
            child_course=groups[Course.OfferingType.ONLINE_PRESENTATION][0],
            price=9,
        )
        payment = Payment.objects.create(
            user=user,
            target_type=Payment.TargetType.COURSE,
            target_id=str(reg.id),
            amount=120,
            currency="IRT",
            metadata={"reg_id": reg.id},
        )
        preserved = (reg.id, item.id, payment.id, reg.status, reg.price)
        migration = importlib.import_module(
            "presentations.migrations.0011_seed_all_access_bundles"
        )

        migration.seed_bundles(django_apps, None)
        migration.seed_bundles(django_apps, None)

        reg.refresh_from_db()
        item.refresh_from_db()
        payment.refresh_from_db()
        reactivated_component.refresh_from_db()
        self.assertEqual(
            (reg.id, item.id, payment.id, reg.status, reg.price),
            preserved,
        )
        self.assertTrue(_user_has_access_to_course(user, item.child_course))
        self.assertTrue(reactivated_component.is_active)
        self.assertEqual(
            reactivated_component.updated_at,
            original_component_updated_at,
        )
        bundles = Course.objects.filter(bundle_type__in=Course.BundleType.values)
        self.assertEqual(bundles.count(), 3)
        for bundle in bundles:
            config = BUNDLE_CATALOG[bundle.bundle_type]
            self.assertEqual(bundle.slug, config["slug"])
            self.assertEqual(bundle.price, config["price"])
            self.assertIsNone(bundle.capacity)
            self.assertEqual(bundle.children.count(), config["expected_member_count"])
            expected_rules = (
                {(1, time(17), time(20)), (6, time(17), time(20))}
                if bundle.bundle_type == Course.BundleType.ALL_ONLINE_PRESENTATIONS
                else {(3, time(9), time(12))}
            )
            for course in [bundle, *bundle.children.all()]:
                self.assertEqual(
                    set(
                        course.schedule.values_list(
                            "weekday", "start_time", "end_time"
                        )
                    ),
                    expected_rules,
                )
        self.assertEqual(
            ScheduleRule.objects.filter(
                course__in=groups[Course.OfferingType.ONLINE_PRESENTATION]
            ).count(),
            12,
        )

    def test_populated_ambiguous_catalogue_fails_before_writing(self):
        self.make_component(Course.OfferingType.ONLINE_PRESENTATION, 0)
        migration = importlib.import_module(
            "presentations.migrations.0011_seed_all_access_bundles"
        )
        with self.assertRaisesMessage(RuntimeError, "expected 6, found 1"):
            migration.seed_bundles(django_apps, None)
        self.assertFalse(Course.objects.filter(bundle_type__isnull=False).exists())

    def test_registered_legacy_stable_slug_collision_fails_without_relabeling_history(self):
        groups = {
            Course.OfferingType.ONLINE_PRESENTATION: [
                self.make_component(Course.OfferingType.ONLINE_PRESENTATION, i)
                for i in range(6)
            ],
            Course.OfferingType.IN_PERSON_WORKSHOP: [
                self.make_component(Course.OfferingType.IN_PERSON_WORKSHOP, i)
                for i in range(3)
            ],
            Course.OfferingType.ONLINE_WORKSHOP: [
                self.make_component(Course.OfferingType.ONLINE_WORKSHOP, i)
                for i in range(3)
            ],
        }
        legacy = Course.objects.create(
            name="Historical slug collision",
            slug="online-presentations-bundle",
            price=1,
            capacity=None,
        )
        user = get_user_model().objects.create_user(email="collision@example.com")
        reg = Registration.objects.create(
            course=legacy,
            user=user,
            price=1,
            status=Registration.Status.FINAL,
        )
        migration = importlib.import_module(
            "presentations.migrations.0011_seed_all_access_bundles"
        )

        with self.assertRaisesMessage(RuntimeError, "existing legacy course"):
            migration.seed_bundles(django_apps, None)

        reg.refresh_from_db()
        legacy.refresh_from_db()
        self.assertEqual(reg.course_id, legacy.id)
        self.assertIsNone(legacy.bundle_type)
        self.assertEqual(
            sum(len(values) for values in groups.values()),
            12,
        )


class BundleAdminAndValidationTests(BundleTestMixin, TestCase):
    def test_registration_admin_supports_bundle_filter_and_reporting(self):
        registration_admin = RegistrationAdmin(Registration, AdminSite())
        self.assertIn("course__bundle_type", registration_admin.list_filter)
        self.assertIn("bundle_type", registration_admin.list_display)
        self.assertIn("member_snapshot", registration_admin.list_display)
        self.assertIn("effective_remaining_capacity", registration_admin.list_display)

    def test_admin_form_rejects_empty_bundle_composition(self):
        config = BUNDLE_CATALOG[Course.BundleType.ALL_ONLINE_WORKSHOPS]
        bundle = Course.objects.create(
            name="Admin invalid bundle",
            slug="admin-invalid-bundle",
            bundle_type=Course.BundleType.ALL_ONLINE_WORKSHOPS,
            price=config["price"],
        )
        form = CourseAdminForm(
            data={
                "name": bundle.name,
                "subtitle": "",
                "description": "",
                "presenters": [],
                "start_date": "",
                "online": True,
                "onsite": False,
                "classes_count": 0,
                "offering_type": "",
                "bundle_type": bundle.bundle_type,
                "capacity": "",
                "price": bundle.price,
                "children": [],
                "requires_approval": False,
                "slug": bundle.slug,
                "is_active": True,
            },
            instance=bundle,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("children", form.errors)

    def test_overlapping_active_bundle_membership_is_blocked(self):
        first, members = self.make_bundle(
            Course.BundleType.ALL_ONLINE_WORKSHOPS,
            slug_suffix="-first",
        )
        second = Course.objects.create(
            name="Second online workshop bundle",
            slug="second-online-workshop-bundle",
            bundle_type=Course.BundleType.ALL_ONLINE_WORKSHOPS,
            price=299_000,
        )
        with self.assertRaises(ValidationError), transaction.atomic():
            second.children.add(members[0])
        self.assertTrue(first.children.filter(pk=members[0].pk).exists())

    def test_registered_course_deletion_is_protected(self):
        bundle, _ = self.make_bundle(slug_suffix="-protected")
        Registration.objects.create(
            course=bundle,
            user=self.make_user("protected"),
            price=bundle.price,
            status=Registration.Status.CANCELLED,
        )
        with self.assertRaises(ProtectedError):
            bundle.delete()


@skipUnlessDBFeature("has_select_for_update")
class BundleConcurrencyTests(BundleTestMixin, TransactionTestCase):
    reset_sequences = True

    def test_concurrent_redemptions_cannot_exceed_global_limit(self):
        bundles = [
            self.make_bundle(Course.BundleType.ALL_ONLINE_PRESENTATIONS)[0],
            self.make_bundle(Course.BundleType.ALL_ONLINE_WORKSHOPS)[0],
        ]
        users = [self.make_user(f"discount-race-{index}") for index in range(2)]
        discount = DiscountCode.objects.create(
            code="race",
            amount_off=10_000,
            max_uses=1,
        )
        barrier = Barrier(2)
        result_lock = Lock()
        results = []

        def register(course_id, user_id):
            close_old_connections()
            try:
                barrier.wait()
                registration = submit_registration(
                    course=Course.objects.get(pk=course_id),
                    user=get_user_model().objects.get(pk=user_id),
                    discount_code=discount.code,
                )
                value = registration.status
            except CustomAPIException as exc:
                value = exc.app_code
            finally:
                close_old_connections()
            with result_lock:
                results.append(value)

        threads = [
            Thread(target=register, args=(bundle.id, user.id))
            for bundle, user in zip(bundles, users)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(results.count(Registration.Status.APPROVED), 1)
        self.assertEqual(results.count(EC.DISCOUNT_LIMIT_REACHED), 1)
        discount.refresh_from_db()
        self.assertEqual(discount.used_count, 1)

    def test_concurrent_bundle_submissions_do_not_oversell_any_member(self):
        bundle, members = self.make_bundle(
            Course.BundleType.ALL_ONLINE_WORKSHOPS,
            capacities=[1, 1, 1],
        )
        users = [self.make_user(f"concurrent-{index}") for index in range(2)]
        barrier = Barrier(2)
        result_lock = Lock()
        statuses = []

        def register(user_id):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=user_id)
                barrier.wait()
                value = submit_registration(
                    course=Course.objects.get(pk=bundle.pk), user=user
                ).status
            finally:
                close_old_connections()
            with result_lock:
                statuses.append(value)

        threads = [Thread(target=register, args=(user.id,)) for user in users]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(statuses.count(Registration.Status.APPROVED), 1)
        self.assertEqual(statuses.count(Registration.Status.RESERVED), 1)
        self.assertTrue(all(member.remained_capacity() == 0 for member in members))
        self.assertEqual(
            RegistrationItem.objects.values("registration_id")
            .annotate()
            .count(),
            6,
        )

    def test_concurrent_approvals_are_all_or_none(self):
        bundle, members = self.make_bundle(
            Course.BundleType.ALL_ONLINE_WORKSHOPS,
            capacities=[1, 1, 1],
        )
        regs = []
        for index in range(2):
            reg = Registration.objects.create(
                course=bundle,
                user=self.make_user(f"approval-{index}"),
                price=bundle.price,
                status=Registration.Status.QUEUED,
            )
            RegistrationItem.objects.bulk_create(
                [RegistrationItem(registration=reg, child_course=m, price=0) for m in members]
            )
            regs.append(reg)
        barrier = Barrier(2)
        result_lock = Lock()
        results = []

        def approve(reg_id):
            close_old_connections()
            try:
                barrier.wait()
                set_status_approved(Registration.objects.get(pk=reg_id))
                value = "approved"
            except CustomAPIException as exc:
                value = exc.app_code
            finally:
                close_old_connections()
            with result_lock:
                results.append(value)

        threads = [Thread(target=approve, args=(reg.id,)) for reg in regs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(results.count("approved"), 1)
        self.assertEqual(results.count(EC.REG_CAPACITY_UNAVAILABLE), 1)
        self.assertEqual(
            Registration.objects.filter(status=Registration.Status.APPROVED).count(),
            1,
        )
