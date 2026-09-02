from collections import defaultdict
from datetime import datetime, timedelta

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import status

from acm import error_codes as EC
from acm.exceptions import CustomAPIException
from accounts.models import UserExtraData
from notification.services import (
    send_email_with_custom_template,
    send_status_change_email,
)
from payment.models import Payment
from payment.services import initiate_payment_for_target

from .models import (
    BUNDLE_CATALOG,
    Course,
    CourseSession,
    DiscountCode,
    Registration,
    RegistrationItem,
    _taken_seats,
)

User = get_user_model()

SKYROOM_BASEURL = settings.SKYROOM_BASEURL
SKYROOM_APIKEY = settings.SKYROOM_APIKEY
SKYROOM_ROOMID = settings.SKYROOM_ROOMID
HEADERS = {"accept": "application/json", "content-type": "application/json"}


def _compute_total_amount(reg: Registration) -> int:
    parent = reg.price if reg.price is not None else (reg.course.price or 0)
    children = sum((item.price or 0) for item in reg.items.all())
    return parent + children


def _compose_description(reg: Registration) -> str:
    child_slugs = ", ".join(item.child_course.slug for item in reg.items.all())
    return f"{reg.course.slug} + [{child_slugs}]" if child_slugs else reg.course.slug


def _validate_new_bundle(course: Course, members: list[Course]) -> None:
    if not course.is_active or not course.is_bundle:
        raise CustomAPIException(
            code=EC.REG_OFFERING_UNAVAILABLE,
            message="This product is not available for a new registration.",
            status_code=status.HTTP_409_CONFLICT,
        )
    if (
        course.offering_type is not None
        or course.capacity is not None
        or course.price is None
        or course.slug != BUNDLE_CATALOG[course.bundle_type]["slug"]
        or course.bundle_composition_errors(members)
    ):
        raise CustomAPIException(
            code=EC.REG_PACKAGE_UNAVAILABLE,
            message="This bundle has an invalid or incomplete member composition.",
            status_code=status.HTTP_409_CONFLICT,
        )


def _item_course_ids(reg: Registration) -> list[int]:
    cache = getattr(reg, "_prefetched_objects_cache", {})
    if "items" in cache:
        return [item.child_course_id for item in cache["items"]]
    return list(reg.items.values_list("child_course_id", flat=True))


def _claim_course_ids(reg: Registration) -> set[int]:
    """Capacity-bearing rows claimed by this historical snapshot."""
    item_ids = set(_item_course_ids(reg))
    if reg.course.is_bundle:
        return item_ids
    # Genuine legacy packages retain their historical parent-seat behavior.
    return {reg.course_id, *item_ids}


def _lock_courses(course_ids) -> dict[int, Course]:
    ids = sorted(set(course_ids))
    return {
        course.id: course
        for course in Course.objects.select_for_update().filter(id__in=ids).order_by("id")
    }


def _lock_claimed_courses(registrations: list[Registration]) -> dict[int, Course]:
    ids = set()
    for reg in registrations:
        ids.add(reg.course_id)
        ids.update(_claim_course_ids(reg))
    return _lock_courses(ids)


def _requested_users_by_course(registrations: list[Registration]):
    requested = defaultdict(set)
    for reg in registrations:
        for course_id in _claim_course_ids(reg):
            requested[course_id].add(reg.user_id)
    return requested


def _occupied_user_ids(
    course: Course,
    *,
    exclude_registration_ids=None,
    for_update: bool = False,
) -> set[int]:
    statuses = (Registration.Status.APPROVED, Registration.Status.FINAL)
    direct = Registration.objects.filter(course=course, status__in=statuses)
    items = RegistrationItem.objects.filter(
        child_course=course,
        registration__status__in=statuses,
    )
    if exclude_registration_ids:
        direct = direct.exclude(id__in=exclude_registration_ids)
        items = items.exclude(registration_id__in=exclude_registration_ids)
    if for_update:
        direct = direct.select_for_update()
        items = items.select_for_update()
    return set(direct.values_list("user_id", flat=True)) | set(
        items.values_list("registration__user_id", flat=True)
    )


def _validate_capacity(
    registrations: list[Registration], locked_courses: dict[int, Course]
) -> None:
    """Validate all participant claims while every course row is locked."""
    if not registrations:
        return
    excluded_ids = [reg.id for reg in registrations if reg.id]
    for course_id, requested_users in _requested_users_by_course(registrations).items():
        course = locked_courses.get(course_id)
        if course is None:
            raise CustomAPIException(
                code=EC.REG_PACKAGE_UNAVAILABLE,
                message="The snapshotted bundle composition is incomplete.",
                status_code=status.HTTP_409_CONFLICT,
            )
        if course.capacity is None:
            continue
        occupied_users = _occupied_user_ids(
            course,
            exclude_registration_ids=excluded_ids,
            for_update=True,
        )
        if len(occupied_users | requested_users) > course.capacity:
            raise CustomAPIException(
                code=EC.REG_CAPACITY_UNAVAILABLE,
                message=f"No remaining capacity for {course.name}.",
                status_code=status.HTTP_409_CONFLICT,
            )


def _virtual_bundle_fits(
    *, user_id: int, members: list[Course], locked_courses: dict[int, Course]
) -> bool:
    for member in members:
        locked = locked_courses[member.id]
        if locked.capacity is None:
            continue
        occupied = _occupied_user_ids(locked, for_update=True)
        if len(occupied | {user_id}) > locked.capacity:
            return False
    return True


def _first_waitlisted_locked(
    product: Course, *, exclude_registration_id: int | None = None
) -> Registration | None:
    queryset = (
        Registration.objects.select_for_update()
        .select_related("course", "user")
        .filter(course=product, status=Registration.Status.RESERVED)
        .order_by("submitted_at", "id")
    )
    if exclude_registration_id is not None:
        queryset = queryset.exclude(id=exclude_registration_id)
    return queryset.first()


def _has_relevant_waitlist_priority_locked(reg: Registration) -> bool:
    """Do not let a cancelled retry jump any queue claiming its member seats."""
    if _first_waitlisted_locked(reg.course, exclude_registration_id=reg.id):
        return True
    claim_ids = _claim_course_ids(reg)
    direct_waiters = Registration.objects.select_for_update().filter(
        course_id__in=claim_ids,
        status=Registration.Status.RESERVED,
    ).exclude(pk=reg.pk)
    if direct_waiters.exists():
        return True
    item_waiter_ids = list(
        RegistrationItem.objects.filter(
            child_course_id__in=claim_ids,
            registration__status=Registration.Status.RESERVED,
        )
        .exclude(registration_id=reg.pk)
        .values_list("registration_id", flat=True)
    )
    return Registration.objects.select_for_update().filter(
        id__in=item_waiter_ids,
        status=Registration.Status.RESERVED,
    ).exists()


def _schedule_status_email(
    *,
    to: str,
    status_code: str,
    extra: dict,
    deduplication_key: str | None = None,
) -> None:
    def send_after_commit():
        send_status_change_email(
            to=to,
            status_code=status_code,
            extra=extra,
            deduplication_key=deduplication_key,
        )

    transaction.on_commit(send_after_commit, robust=True)


def _schedule_promotion_email(reg: Registration) -> None:
    def send_after_commit():
        send_email_with_custom_template(
            to=reg.user.email,
            template="course_waitlist_promoted",
            status_code="COURSE_WAITLIST_PROMOTED",
            extra={"course": reg.course.name},
            deduplication_key=f"course-waitlist-promoted:{reg.id}",
        )

    transaction.on_commit(send_after_commit, robust=True)


def _schedule_waitlist_promotions(course_ids) -> None:
    affected_ids = sorted(set(course_ids))
    if not affected_ids:
        return

    def promote_after_commit():
        from .tasks import promote_waitlist_task

        promote_waitlist_task.delay(affected_ids)

    transaction.on_commit(promote_after_commit, robust=True)


def _progress_locked_registration(
    reg: Registration,
    *,
    override_amount: int | None = None,
    promoted: bool = False,
) -> Registration:
    """Progress an existing, already-validated snapshot without sale validation."""
    amount = (
        override_amount
        if override_amount is not None
        else _compute_total_amount(reg)
    )
    now = timezone.now()
    reg.status = (
        Registration.Status.FINAL if amount <= 0 else Registration.Status.APPROVED
    )
    reg.payment_link = ""
    reg.decided_at = now
    reg.save(update_fields=["status", "payment_link", "decided_at"])

    if promoted:
        _schedule_promotion_email(reg)
    else:
        _schedule_status_email(
            to=reg.user.email,
            status_code=(
                "COURSE_REQUEST_FINAL"
                if reg.status == Registration.Status.FINAL
                else "COURSE_REQUEST_APPROVED"
            ),
            extra={"course": reg.course.name},
        )
    return reg


@transaction.atomic
def submit_registration(
    *,
    course: Course,
    user: User,
    extra_updates: dict | None = None,
    child_ids: list[int] | None = None,
    resume_url: str | None = None,
    discount_code: str | None = None,
) -> Registration:
    """Snapshot and allocate an indivisible current bundle, or waitlist it."""
    if (not user.is_authenticated) or not getattr(user, "is_email_verified", False):
        raise CustomAPIException(
            code=EC.ACC_EMAIL_NOT_VERIFIED,
            message="Login with verified email required",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if child_ids is not None:
        raise CustomAPIException(
            code=EC.REG_PACKAGE_UNAVAILABLE,
            message="child_ids is not accepted; the server snapshots every bundle member.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    candidate = (
        Course.objects.prefetch_related("children__parents")
        .filter(pk=course.pk)
        .first()
    )
    if candidate is None:
        raise CustomAPIException(
            code=EC.REG_OFFERING_UNAVAILABLE,
            message="This product is unavailable.",
            status_code=status.HTTP_409_CONFLICT,
        )

    existing = (
        Registration.objects.select_related("course")
        .prefetch_related("items")
        .filter(course_id=candidate.id, user=user)
        .first()
    )
    members = list(candidate.bundle_members())
    tentative_claim_ids = (
        _claim_course_ids(existing)
        if existing is not None
        else {member.id for member in members}
    )
    locked_courses = _lock_courses({candidate.id, *tentative_claim_ids})
    course = locked_courses[candidate.id]
    reg = (
        Registration.objects.select_for_update()
        .select_related("course", "user")
        .prefetch_related("items__child_course")
        .filter(course=course, user=user)
        .first()
    )

    if reg and reg.status in (Registration.Status.APPROVED, Registration.Status.FINAL):
        raise CustomAPIException(
            code=EC.REG_ALREADY_FINAL_OR_APPROVED,
            message="You already have an approved registration for this bundle.",
            status_code=status.HTTP_409_CONFLICT,
        )
    if reg and reg.status == Registration.Status.RESERVED:
        # Preserve FIFO timestamp, price and item snapshot byte-for-byte.
        if extra_updates:
            _update_user_extra_data(user=user, extra_updates=extra_updates)
        _schedule_waitlist_promotions(_claim_course_ids(reg))
        return reg

    # New purchases are validated against the current composition. Existing
    # legacy rows use their own snapshot in every later lifecycle operation.
    candidate = Course.objects.prefetch_related("children__parents").get(pk=course.pk)
    members = list(candidate.bundle_members())
    _validate_new_bundle(course, members)
    if {member.id for member in members} != set(tentative_claim_ids):
        raise CustomAPIException(
            code=EC.REG_PACKAGE_UNAVAILABLE,
            message="Bundle membership changed during registration; please retry.",
            status_code=status.HTTP_409_CONFLICT,
        )

    if reg is None:
        reg = Registration(course=course, user=user)
    waitlist_has_priority = _first_waitlisted_locked(
        course, exclude_registration_id=reg.id
    ) is not None
    capacity_available = _virtual_bundle_fits(
        user_id=user.id,
        members=members,
        locked_courses=locked_courses,
    )

    reg.resume_url = resume_url or reg.resume_url
    reg.price, reg.discount_code = _reserve_discount_for_registration(
        registration=reg,
        course=course,
        raw_code=discount_code,
    )
    reg.submitted_at = timezone.now()
    reg.rejection_reason = ""
    reg.payment_link = ""
    reg.decided_at = None
    reg.status = (
        Registration.Status.RESERVED
        if waitlist_has_priority or not capacity_available
        else Registration.Status.QUEUED
    )
    reg.save()
    RegistrationItem.objects.filter(registration=reg).delete()
    RegistrationItem.objects.bulk_create(
        [
            RegistrationItem(registration=reg, child_course=member, price=0)
            for member in members
        ]
    )

    if extra_updates:
        _update_user_extra_data(user=user, extra_updates=extra_updates)
    _schedule_status_email(
        to=user.email,
        status_code="COURSE_REQUEST_SUBMITTED",
        extra={
            "course": course.name,
            "status": reg.status,
            "waitlist_position": reg.waitlist_position(),
        },
    )
    if reg.status == Registration.Status.QUEUED:
        reg = _progress_locked_registration(
            reg,
            override_amount=_compute_total_amount(reg),
        )
    return reg


def _update_user_extra_data(*, user: User, extra_updates: dict) -> None:
    extra, _ = UserExtraData.objects.get_or_create(user=user)
    extra.answers = {**(extra.answers or {}), **extra_updates}
    if "codeforces_score" in extra_updates:
        try:
            extra.codeforces_score = int(extra_updates["codeforces_score"])
        except (TypeError, ValueError):
            pass
    if "codeforces_handle" in extra_updates:
        extra.codeforces_handle = str(extra_updates["codeforces_handle"])[:64]
    extra.save()


def _load_registration_for_lock(registration_id: int) -> Registration:
    return (
        Registration.objects.select_related("course", "user", "discount_code")
        .prefetch_related("items__child_course")
        .get(pk=registration_id)
    )


@transaction.atomic
def set_status_approved(
    reg: Registration,
    *,
    actor: User | None = None,
    override_amount: int | None = None,
) -> Registration:
    tentative = _load_registration_for_lock(reg.id)
    locked_courses = _lock_claimed_courses([tentative])
    reg = (
        Registration.objects.select_for_update()
        .select_related("course", "user", "discount_code")
        .prefetch_related("items__child_course")
        .get(pk=reg.pk)
    )
    if reg.status in (Registration.Status.APPROVED, Registration.Status.FINAL):
        return reg
    first_waitlisted = _first_waitlisted_locked(reg.course)
    if first_waitlisted is not None and first_waitlisted.id != reg.id:
        raise CustomAPIException(
            code=EC.REG_CAPACITY_UNAVAILABLE,
            message="Earlier waitlisted registrations have priority for the next slot.",
            status_code=status.HTTP_409_CONFLICT,
        )
    _validate_capacity([reg], locked_courses)
    return _progress_locked_registration(reg, override_amount=override_amount)


@transaction.atomic
def initiate_registration_payment(*, registration_id: int, user: User):
    tentative = (
        Registration.objects.select_related("course", "user", "discount_code")
        .prefetch_related("items__child_course")
        .filter(id=registration_id, user=user)
        .first()
    )
    if tentative is None:
        raise CustomAPIException(
            code=EC.PAY_NOT_OWNED,
            message="Registration not found for this user.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    locked_courses = _lock_claimed_courses([tentative])
    reg = (
        Registration.objects.select_for_update()
        .select_related("course", "user", "discount_code")
        .prefetch_related("items__child_course")
        .get(id=registration_id, user=user)
    )
    if reg.status == Registration.Status.CANCELLED:
        if _has_relevant_waitlist_priority_locked(reg):
            raise CustomAPIException(
                code=EC.REG_CAPACITY_UNAVAILABLE,
                message="Waitlisted registrations have priority for the member seats.",
                status_code=status.HTTP_409_CONFLICT,
            )
        _validate_capacity([reg], locked_courses)
        reg.status = Registration.Status.APPROVED
        reg.payment_link = ""
        reg.decided_at = timezone.now()
        reg.save(update_fields=["status", "payment_link", "decided_at"])

    if reg.status != Registration.Status.APPROVED:
        raise CustomAPIException(
            code=EC.REG_PAYMENT_NOT_AVAILABLE,
            message="Payment is available only for approved registrations.",
            status_code=status.HTTP_409_CONFLICT,
        )
    amount = _compute_total_amount(reg)
    if amount <= 0:
        raise CustomAPIException(
            code=EC.REG_PAYMENT_NOT_AVAILABLE,
            message="This registration does not require payment.",
            status_code=status.HTTP_409_CONFLICT,
        )
    result = initiate_payment_for_target(
        user=reg.user,
        target_type=Payment.TargetType.COURSE,
        target_id=str(reg.id),
        amount=amount,
        description=_compose_description(reg),
        extra_metadata={
            "reg_id": reg.id,
            "course_id": reg.course_id,
            "discount_code": reg.discount_code.code if reg.discount_code_id else None,
        },
    )
    reg.payment_link = result.url
    reg.save(update_fields=["payment_link"])
    return result


def _affected_queue_product_ids(course_ids) -> list[int]:
    affected = set(course_ids)
    product_ids = set(
        Registration.objects.filter(
            course_id__in=affected,
            status=Registration.Status.RESERVED,
        ).values_list("course_id", flat=True)
    )
    product_ids.update(
        Course.objects.filter(
            is_active=True,
            bundle_type__isnull=False,
            children__id__in=affected,
            registrations__status=Registration.Status.RESERVED,
        ).values_list("id", flat=True)
    )
    product_ids.update(
        Course.objects.filter(
            id__in=affected,
            registrations__status=Registration.Status.RESERVED,
        ).values_list("id", flat=True)
    )
    return sorted(product_ids)


@transaction.atomic
def _promote_product_waitlist(product_id: int) -> list[Registration]:
    tentative = list(
        Registration.objects.select_related("course", "user")
        .prefetch_related("items__child_course")
        .filter(course_id=product_id, status=Registration.Status.RESERVED)
        .order_by("submitted_at", "id")
    )
    if not tentative:
        return []
    locked_courses = _lock_claimed_courses(tentative)
    candidates = list(
        Registration.objects.select_for_update()
        .select_related("course", "user")
        .prefetch_related("items__child_course")
        .filter(course_id=product_id, status=Registration.Status.RESERVED)
        .order_by("submitted_at", "id")
    )
    promoted = []
    for reg in candidates:
        try:
            _validate_capacity([reg], locked_courses)
        except CustomAPIException as exc:
            if exc.app_code == EC.REG_CAPACITY_UNAVAILABLE:
                break
            raise
        promoted.append(
            _progress_locked_registration(
                reg,
                override_amount=_compute_total_amount(reg),
                promoted=True,
            )
        )
    return promoted


def promote_waitlists(*, course_ids) -> list[Registration]:
    promoted = []
    for product_id in _affected_queue_product_ids(course_ids):
        promoted.extend(_promote_product_waitlist(product_id))
    return promoted


def promote_waitlist(*, course_id: int) -> list[Registration]:
    """Compatibility wrapper: promote direct and affected current bundle queues."""
    return promote_waitlists(course_ids=[course_id])


@transaction.atomic
def set_status_final(
    regs: list[Registration], *, actor: User | None = None
) -> list[Registration]:
    if isinstance(regs, Registration):
        regs = [regs]
    reg_ids = [reg.id for reg in regs]
    if not reg_ids:
        return []
    tentative = list(
        Registration.objects.select_related("course", "user")
        .prefetch_related("items__child_course")
        .filter(id__in=reg_ids)
    )
    locked_courses = _lock_claimed_courses(tentative)
    locked_by_id = {
        reg.id: reg
        for reg in Registration.objects.select_for_update()
        .select_related("course", "user")
        .prefetch_related("items__child_course")
        .filter(id__in=reg_ids)
    }
    regs = [locked_by_id[reg_id] for reg_id in reg_ids]
    pending = [reg for reg in regs if reg.status != Registration.Status.FINAL]
    if any(reg.status != Registration.Status.APPROVED for reg in pending):
        raise CustomAPIException(
            code=EC.REG_APPROVAL_REQUIRED,
            message="Only payment-eligible registrations can be finalized.",
            status_code=status.HTTP_409_CONFLICT,
        )
    _validate_capacity(pending, locked_courses)
    for reg in pending:
        reg.status = Registration.Status.FINAL
        reg.decided_at = timezone.now()
        reg.save(update_fields=["status", "decided_at"])
        _schedule_status_email(
            to=reg.user.email,
            status_code="COURSE_REQUEST_FINAL",
            extra={"course": reg.course.name},
        )
    return regs


@transaction.atomic
def cancel_registration_for_failed_payment(registration_id: int, *, user: User) -> None:
    tentative = (
        Registration.objects.select_related("course", "user")
        .prefetch_related("items__child_course")
        .filter(id=registration_id, user=user)
        .first()
    )
    if tentative is None:
        return
    _lock_claimed_courses([tentative])
    reg = (
        Registration.objects.select_for_update()
        .select_related("course")
        .prefetch_related("items")
        .filter(
            id=registration_id,
            user=user,
            status=Registration.Status.APPROVED,
        )
        .first()
    )
    if reg is None:
        return
    affected = _claim_course_ids(reg)
    reg.status = Registration.Status.CANCELLED
    reg.payment_link = ""
    reg.decided_at = timezone.now()
    reg.save(update_fields=["status", "payment_link", "decided_at"])
    _schedule_waitlist_promotions(affected)


@transaction.atomic
def set_status_rejected(
    reg: Registration, *, actor: User | None = None
) -> Registration:
    tentative = _load_registration_for_lock(reg.id)
    _lock_claimed_courses([tentative])
    reg = (
        Registration.objects.select_for_update()
        .select_related("course", "user")
        .prefetch_related("items")
        .get(pk=reg.pk)
    )
    if reg.status in (Registration.Status.APPROVED, Registration.Status.FINAL):
        raise CustomAPIException(
            code=EC.REG_ALREADY_FINAL_OR_APPROVED,
            message="Approved or finalized registrations cannot be rejected.",
            status_code=status.HTTP_409_CONFLICT,
        )
    if not reg.rejection_reason:
        raise CustomAPIException(
            code=EC.REG_REJECTION_REASON_REQUIRED,
            message="rejection_reason must be set before rejecting",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    was_waitlisted = reg.status == Registration.Status.RESERVED
    affected = _claim_course_ids(reg)
    reg.status = Registration.Status.REJECTED
    reg.decided_at = timezone.now()
    reg.save(update_fields=["status", "decided_at"])
    _schedule_status_email(
        to=reg.user.email,
        status_code="COURSE_REQUEST_REJECTED",
        extra={"course": reg.course.name, "reason": reg.rejection_reason},
    )
    if was_waitlisted:
        _schedule_waitlist_promotions(affected)
    return reg


def get_course_sessions(user: User, course: Course):
    if not _user_has_access_to_course(user, course):
        return None
    return CourseSession.objects.filter(course=course).values()


def _user_has_access_to_course(user, course) -> bool:
    return (
        Registration.objects.filter(
            user=user,
            status=Registration.Status.FINAL,
            course=course,
        ).exists()
        or Registration.objects.filter(
            user=user,
            status=Registration.Status.FINAL,
            items__child_course=course,
        ).exists()
        or Registration.objects.filter(
            user=user,
            status=Registration.Status.FINAL,
            course__bundle_type__isnull=True,
            course__children=course,
        ).exists()
    )


def _now_in_shift_window(
    course, *, window_minutes: int = 15, now: datetime | None = None
) -> bool:
    if now is None:
        now = timezone.localtime()
    rules = course.schedule.filter(weekday=now.weekday())
    for rule in rules:
        start_dt = timezone.make_aware(
            datetime.combine(now.date(), rule.start_time), now.tzinfo
        )
        end_dt = timezone.make_aware(
            datetime.combine(now.date(), rule.end_time), now.tzinfo
        )
        if start_dt - timedelta(minutes=window_minutes) <= now <= end_dt + timedelta(
            minutes=window_minutes
        ):
            return True
    return False


def create_skyroom_link(user: User, course: Course) -> str | None:
    if not _user_has_access_to_course(user, course):
        return None
    if not _now_in_shift_window(course, window_minutes=15):
        return None
    return get_skyroom_presentation_link(
        room_id=SKYROOM_ROOMID,
        user_id=user.email,
        nickname=f"{user.first_name} {user.last_name}",
    )


def get_skyroom_presentation_link(
    room_id: int = 1,
    user_id: str = "sina",
    nickname: str = "Sina",
    language: str = "fa",
    ttl: int = 5400,
):
    url = f"{SKYROOM_BASEURL}/skyroom/api/{SKYROOM_APIKEY}"
    payload = {
        "action": "createLoginUrl",
        "params": {
            "room_id": room_id,
            "user_id": user_id,
            "nickname": nickname,
            "access": 1,
            "concurrent": 1,
            "language": language,
            "ttl": ttl,
        },
    }
    response = requests.post(url, json=payload, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.json().get("result", None)


class InvalidDiscountCode(CustomAPIException):
    def __init__(self, *, error_code: int, message: str):
        super().__init__(
            code=error_code,
            message=message,
            status_code=status.HTTP_400_BAD_REQUEST,
        )


def _get_discount_error(
    discount: DiscountCode,
    course: Course,
    *,
    owns_redemption: bool = False,
) -> tuple[int, str] | None:
    now = timezone.now()
    if not discount.is_active:
        return EC.DISCOUNT_INVALID, "This discount code is inactive."
    if (discount.valid_from and now < discount.valid_from) or (
        discount.valid_until and now > discount.valid_until
    ):
        return EC.DISCOUNT_EXPIRED, "This discount code is not currently valid."
    if discount.course_id and discount.course_id != course.id:
        return EC.DISCOUNT_NOT_APPLICABLE, "This code does not apply to this bundle."
    if (
        not owns_redemption
        and discount.max_uses is not None
        and discount.used_count >= discount.max_uses
    ):
        return EC.DISCOUNT_LIMIT_REACHED, "This discount code has reached its usage limit."
    return None


def _validate_discount_for_course(
    discount: DiscountCode,
    course: Course,
    *,
    owns_redemption: bool = False,
    user: User | None = None,
    registration_id: int | None = None,
) -> int:
    if not course.is_active or not course.is_bundle or course.price is None:
        raise InvalidDiscountCode(
            error_code=EC.DISCOUNT_NOT_APPLICABLE,
            message="Discount codes can only be used for an active bundle.",
        )
    error = _get_discount_error(
        discount,
        course,
        owns_redemption=owns_redemption,
    )
    if error:
        error_code, message = error
        raise InvalidDiscountCode(error_code=error_code, message=message)
    if user is not None and not owns_redemption:
        previous_use = Registration.objects.filter(
            user=user,
            discount_code=discount,
        )
        if registration_id is not None:
            previous_use = previous_use.exclude(pk=registration_id)
        if previous_use.exists():
            raise InvalidDiscountCode(
                error_code=EC.DISCOUNT_ALREADY_USED,
                message="You have already used this discount code.",
            )
    return discount.apply(course.price)


def _discount_id_for_code(raw_code: str) -> int | None:
    code = DiscountCode.normalize_code(raw_code)
    if not code:
        return None
    return (
        DiscountCode.objects.filter(code__iexact=code)
        .values_list("id", flat=True)
        .first()
    )


def _reserve_discount_for_registration(
    *,
    registration: Registration,
    course: Course,
    raw_code: str | None,
) -> tuple[int, DiscountCode | None]:
    """Atomically replace a registration's discount reservation and snapshot price."""
    requested_code = DiscountCode.normalize_code(raw_code)
    requested_id = _discount_id_for_code(requested_code) if requested_code else None
    if requested_code and requested_id is None:
        raise InvalidDiscountCode(
            error_code=EC.DISCOUNT_INVALID,
            message="Discount code not found.",
        )

    previous_id = registration.discount_code_id
    discount_ids = sorted({value for value in (previous_id, requested_id) if value})
    locked_by_id = {
        discount.id: discount
        for discount in DiscountCode.objects.select_for_update()
        .filter(id__in=discount_ids)
        .order_by("id")
    }
    requested = locked_by_id.get(requested_id)
    if requested_id and requested is None:
        raise InvalidDiscountCode(
            error_code=EC.DISCOUNT_INVALID,
            message="Discount code not found.",
        )

    final_price = course.price
    if requested is not None:
        final_price = _validate_discount_for_course(
            requested,
            course,
            owns_redemption=requested_id == previous_id,
            user=registration.user,
            registration_id=registration.id,
        )

    if previous_id != requested_id:
        previous = locked_by_id.get(previous_id)
        if previous is not None:
            previous.used_count = max(previous.used_count - 1, 0)
            previous.save(update_fields=["used_count"])
        if requested is not None:
            requested.used_count += 1
            requested.save(update_fields=["used_count"])

    return final_price, requested


def validate_and_apply_discount(course: Course, code: str):
    """Return the discounted price and code, or raise InvalidDiscountCode."""
    normalized = DiscountCode.normalize_code(code)
    discount = DiscountCode.objects.filter(code__iexact=normalized).first()
    if discount is None:
        raise InvalidDiscountCode(
            error_code=EC.DISCOUNT_INVALID,
            message="Discount code not found.",
        )
    return _validate_discount_for_course(discount, course), discount
