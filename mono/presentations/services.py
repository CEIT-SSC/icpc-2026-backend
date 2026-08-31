from collections import Counter
from datetime import datetime, timedelta

import requests
from django.db import transaction
from django.utils import timezone
from django.contrib.auth import get_user_model

from rest_framework import status
from acm.exceptions import CustomAPIException
from acm import error_codes as EC

from payment.models import Payment
from payment.services import initiate_payment_for_target
from .models import (
    Course,
    CourseSession,
    Registration,
    RegistrationItem,
    _taken_seats,
)
from notification.services import (
    send_email_with_custom_template,
    send_status_change_email,
)
from accounts.models import UserExtraData
from django.conf import settings

User = get_user_model()

SKYROOM_BASEURL = settings.SKYROOM_BASEURL
SKYROOM_APIKEY = settings.SKYROOM_APIKEY
SKYROOM_ROOMID = settings.SKYROOM_ROOMID
HEADERS = {"accept": "application/json", "content-type": "application/json"}


def _compute_total_amount(reg: Registration) -> int:
    parent = reg.price if reg.price is not None else (reg.course.price or 0)
    children = sum((i.price or 0) for i in reg.items.all())
    return parent + children


def _compose_description(reg: Registration) -> str:
    child_slugs = ", ".join(i.child_course.slug for i in reg.items.all())
    if child_slugs:
        return f"{reg.course.slug} + [{child_slugs}]"
    return reg.course.slug


def _validate_individual_offering(course: Course, child_ids: list[int]) -> None:
    if child_ids:
        raise CustomAPIException(
            code=EC.REG_PACKAGE_UNAVAILABLE,
            message="Package purchases are not available in the current offering catalogue.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if (
        not course.is_active
        or course.offering_type not in Course.OfferingType.values
    ):
        raise CustomAPIException(
            code=EC.REG_OFFERING_UNAVAILABLE,
            message="This offering is not available for individual purchase.",
            status_code=status.HTTP_409_CONFLICT,
        )


def _capacity_claims(registrations: list[Registration]) -> Counter:
    claims = Counter(reg.course_id for reg in registrations)
    claims.update(
        RegistrationItem.objects.filter(
            registration_id__in=[reg.id for reg in registrations]
        ).values_list("child_course_id", flat=True)
    )
    return claims


def _lock_claimed_courses(registrations: list[Registration]) -> dict[int, Course]:
    if not registrations:
        return {}

    claims = _capacity_claims(registrations)
    return {
        course.id: course
        for course in Course.objects.select_for_update()
        .filter(id__in=sorted(claims))
        .order_by("id")
    }


def _validate_capacity(
    registrations: list[Registration], locked_courses: dict[int, Course]
) -> None:
    """Validate a batch while all claimed course rows are already locked."""
    if not registrations:
        return

    claims = _capacity_claims(registrations)
    excluded_ids = [reg.id for reg in registrations]

    for course_id, requested_seats in claims.items():
        course = locked_courses[course_id]
        if course.capacity is None:
            continue
        occupied = _taken_seats(
            course,
            exclude_registration_ids=excluded_ids,
            for_update=True,
        )
        if occupied + requested_seats > course.capacity:
            raise CustomAPIException(
                code=EC.REG_CAPACITY_UNAVAILABLE,
                message=f"No remaining capacity for {course.name}.",
                status_code=status.HTTP_409_CONFLICT,
            )


def _available_slots_locked(course: Course) -> int | None:
    """Return available seats while the caller holds the course row lock."""
    if course.capacity is None:
        return None
    occupied = _taken_seats(course, for_update=True)
    return max(course.capacity - occupied, 0)


def _first_waitlisted_locked(
    course: Course, *, exclude_registration_id: int | None = None
) -> Registration | None:
    queryset = (
        Registration.objects.select_for_update()
        .select_related("course", "user")
        .filter(course=course, status=Registration.Status.RESERVED)
        .order_by("submitted_at", "id")
    )
    if exclude_registration_id is not None:
        queryset = queryset.exclude(id=exclude_registration_id)
    return queryset.first()


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
    to = reg.user.email
    course_name = reg.course.name
    deduplication_key = f"course-waitlist-promoted:{reg.id}"

    def send_after_commit():
        send_email_with_custom_template(
            to=to,
            template="course_waitlist_promoted",
            status_code="COURSE_WAITLIST_PROMOTED",
            extra={
                "course": course_name,
            },
            deduplication_key=deduplication_key,
        )

    transaction.on_commit(send_after_commit, robust=True)


def _schedule_waitlist_promotion(course_id: int) -> None:
    def promote_after_commit():
        from .tasks import promote_waitlist_task

        promote_waitlist_task.delay(course_id)

    transaction.on_commit(promote_after_commit, robust=True)


def _progress_locked_registration(
    reg: Registration,
    *,
    override_amount: int | None = None,
    promoted: bool = False,
) -> Registration:
    """Claim the locked seat without contacting the payment gateway."""
    _validate_individual_offering(reg.course, [])
    if reg.items.exists():
        raise CustomAPIException(
            code=EC.REG_PACKAGE_UNAVAILABLE,
            message="Package purchases are not available in the current offering catalogue.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    amount = override_amount if override_amount is not None else _compute_total_amount(reg)
    now = timezone.now()
    if amount <= 0:
        reg.status = Registration.Status.FINAL
        reg.payment_link = ""
        reg.decided_at = now
        reg.save(update_fields=["status", "payment_link", "decided_at"])
    else:
        # APPROVED means that the seat is held and the user may explicitly start
        # payment. Gateway initiation is deliberately deferred until that action.
        reg.status = Registration.Status.APPROVED
        reg.payment_link = ""
        reg.decided_at = now
        reg.save(update_fields=["status", "payment_link", "decided_at"])

    if promoted:
        _schedule_promotion_email(reg)
    else:
        email_status = (
            "COURSE_REQUEST_FINAL"
            if reg.status == Registration.Status.FINAL
            else "COURSE_REQUEST_APPROVED"
        )
        _schedule_status_email(
            to=reg.user.email,
            status_code=email_status,
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
) -> Registration:
    """Allocate a seat immediately or create one deterministic FIFO waitlist row."""
    if (not user.is_authenticated) or (not getattr(user, "is_email_verified", False)):
        raise CustomAPIException(
            code=EC.ACC_EMAIL_NOT_VERIFIED,
            message="Login with verified email required",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    child_ids = list(dict.fromkeys(child_ids or []))
    course = Course.objects.select_for_update().get(pk=course.pk)
    _validate_individual_offering(course, child_ids)

    # ----------------------------
    # ALREADY-OWNED GUARD
    # ----------------------------
    # All finalized registrations for this user:
    finalized_regs = Registration.objects.filter(
        user=user,
        status=Registration.Status.FINAL,
    )

    owned_parent_ids = set(finalized_regs.values_list("course_id", flat=True))
    owned_child_ids = set(
        RegistrationItem.objects.filter(registration__in=finalized_regs)
        .values_list("child_course_id", flat=True)
    )
    owned_ids = owned_parent_ids | owned_child_ids

    if course.id in owned_ids:
        raise CustomAPIException(
            code=EC.REG_ALREADY_OWNED,
            message="You already own this presentation.",
            status_code=status.HTTP_409_CONFLICT,
        )

    reg = Registration.objects.select_for_update().filter(
        course=course, user=user
    ).first()
    if reg and reg.status in (
        Registration.Status.APPROVED,
        Registration.Status.FINAL,
    ):
        raise CustomAPIException(
            code=EC.REG_ALREADY_FINAL_OR_APPROVED,
            message="You already have an approved registration for this presentation.",
            status_code=status.HTTP_409_CONFLICT,
        )
    if reg and reg.status == Registration.Status.RESERVED:
        # Repeated/concurrent submissions are idempotent and do not move a user
        # to the back of the queue or send duplicate submission notifications.
        if resume_url and resume_url != reg.resume_url:
            reg.resume_url = resume_url
            reg.save(update_fields=["resume_url"])
        if extra_updates:
            _update_user_extra_data(user=user, extra_updates=extra_updates)
        if _available_slots_locked(course) not in (0,):
            _schedule_waitlist_promotion(course.id)
        return reg
    if reg is None:
        reg = Registration(course=course, user=user)

    waitlist_has_priority = _first_waitlisted_locked(
        course, exclude_registration_id=reg.id
    ) is not None
    available_slots = _available_slots_locked(course)
    capacity_available = available_slots is None or available_slots > 0

    reg.resume_url = resume_url or reg.resume_url
    reg.price = course.price
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

    if extra_updates:
        _update_user_extra_data(user=user, extra_updates=extra_updates)

    RegistrationItem.objects.filter(registration=reg).delete()

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

@transaction.atomic
def set_status_approved(
    reg: Registration,
    *,
    actor: User | None = None,
    override_amount: int | None = None,
) -> Registration:
    course = Course.objects.select_for_update().get(pk=reg.course_id)
    reg = (
        Registration.objects.select_for_update()
        .select_related("course", "user")
        .get(pk=reg.pk)
    )
    if reg.status in (Registration.Status.APPROVED, Registration.Status.FINAL):
        return reg

    first_waitlisted = _first_waitlisted_locked(course)
    if first_waitlisted is not None and first_waitlisted.id != reg.id:
        raise CustomAPIException(
            code=EC.REG_CAPACITY_UNAVAILABLE,
            message="Earlier waitlisted registrations have priority for the next slot.",
            status_code=status.HTTP_409_CONFLICT,
        )

    _validate_capacity([reg], {course.id: course})
    return _progress_locked_registration(
        reg,
        override_amount=override_amount,
    )


@transaction.atomic
def initiate_registration_payment(*, registration_id: int, user: User):
    """Create a gateway payment only after the registration owner asks to pay."""
    course_id = Registration.objects.filter(
        id=registration_id,
        user=user,
    ).values_list("course_id", flat=True).first()
    if course_id is None:
        raise CustomAPIException(
            code=EC.PAY_NOT_OWNED,
            message="Registration not found for this user.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    course = Course.objects.select_for_update().get(pk=course_id)
    reg = (
        Registration.objects.select_for_update()
        .select_related("course", "user")
        .prefetch_related("items__child_course")
        .get(id=registration_id, user=user)
    )

    # A cancelled payment released its seat. A deliberate retry may reclaim it,
    # but only if capacity and FIFO waitlist priority still allow it.
    if reg.status == Registration.Status.CANCELLED:
        first_waitlisted = _first_waitlisted_locked(course)
        if first_waitlisted is not None:
            raise CustomAPIException(
                code=EC.REG_CAPACITY_UNAVAILABLE,
                message="Waitlisted registrations have priority for the next slot.",
                status_code=status.HTTP_409_CONFLICT,
            )
        _validate_capacity([reg], {course.id: course})
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
        },
    )
    reg.payment_link = result.url
    reg.save(update_fields=["payment_link"])
    return result


@transaction.atomic
def promote_waitlist(*, course_id: int) -> list[Registration]:
    """Promote exactly the oldest waitlisted rows that fit available capacity."""
    course = Course.objects.select_for_update().get(pk=course_id)
    available_slots = _available_slots_locked(course)
    if available_slots == 0:
        return []

    candidates = (
        Registration.objects.select_for_update()
        .select_related("course", "user")
        .filter(course=course, status=Registration.Status.RESERVED)
        .order_by("submitted_at", "id")
    )
    if available_slots is not None:
        candidates = candidates[:available_slots]

    promoted = []
    for reg in list(candidates):
        _validate_capacity([reg], {course.id: course})
        promoted.append(
            _progress_locked_registration(
                reg,
                override_amount=_compute_total_amount(reg),
                promoted=True,
            )
        )
    return promoted


@transaction.atomic
def set_status_final(regs: list[Registration], *, actor: User | None = None) -> list[Registration]:
    if isinstance(regs, Registration):
        regs = [regs]
    reg_ids = [reg.id for reg in regs]
    if not reg_ids:
        return []

    locked_courses = _lock_claimed_courses(regs)
    locked_by_id = {
        reg.id: reg
        for reg in Registration.objects.select_for_update()
        .select_related("course", "user")
        .filter(id__in=reg_ids)
    }
    regs = [locked_by_id[reg_id] for reg_id in reg_ids]
    pending_finalization = [
        reg for reg in regs if reg.status != Registration.Status.FINAL
    ]
    invalid = [
        reg
        for reg in pending_finalization
        if reg.status != Registration.Status.APPROVED
    ]
    if invalid:
        raise CustomAPIException(
            code=EC.REG_APPROVAL_REQUIRED,
            message="Only payment-eligible registrations can be finalized.",
            status_code=status.HTTP_409_CONFLICT,
        )
    _validate_capacity(pending_finalization, locked_courses)

    for reg in pending_finalization:
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
    """Release an APPROVED seat when its gateway payment definitively fails."""
    course_id = Registration.objects.filter(
        id=registration_id, user=user
    ).values_list("course_id", flat=True).first()
    if course_id is None:
        return
    Course.objects.select_for_update().get(pk=course_id)
    reg = Registration.objects.select_for_update().filter(
        id=registration_id,
        user=user,
        status=Registration.Status.APPROVED,
    ).first()
    if reg is None:
        return
    reg.status = Registration.Status.CANCELLED
    reg.payment_link = ""
    reg.decided_at = timezone.now()
    reg.save(update_fields=["status", "payment_link", "decided_at"])
    _schedule_waitlist_promotion(course_id)


@transaction.atomic
def set_status_rejected(reg: Registration, *, actor: User | None = None) -> Registration:
    course = Course.objects.select_for_update().get(pk=reg.course_id)
    reg = (
        Registration.objects.select_for_update()
        .select_related("course", "user")
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
    reg.status = Registration.Status.REJECTED
    reg.decided_at = timezone.now()
    reg.save(update_fields=["status", "decided_at"])
    _schedule_status_email(
        to=reg.user.email,
        status_code="COURSE_REQUEST_REJECTED",
        extra={"course": reg.course.name, "reason": reg.rejection_reason},
    )
    if was_waitlisted:
        _schedule_waitlist_promotion(course.id)
    return reg

def get_course_sessions(user: User, course: Course):
    if not _user_has_access_to_course(user, course):
        return None
    return CourseSession.objects.filter(course=course).values()


def _user_has_access_to_course(user, course) -> bool:
    return (
        # direct child registration
        Registration.objects.filter(
            user=user,
            status=Registration.Status.FINAL,
            course=course,
        ).exists()
        or
        # selected as a child item under a finalized parent registration
        Registration.objects.filter(
            user=user,
            status=Registration.Status.FINAL,
            items__child_course=course,
        ).exists()
        or
        # finalized to a parent (access to all children)
        Registration.objects.filter(
            user=user,
            status=Registration.Status.FINAL,
            course__children=course,
        ).exists()
    )

def _now_in_shift_window(course, *, window_minutes: int = 15, now: datetime | None = None) -> bool:
    if now is None:
        now = timezone.localtime()

    today_weekday = now.weekday()  # Monday=0
    rules = course.schedule.filter(weekday=today_weekday)

    if not rules.exists():
        return False

    # build datetimes for today using the course's schedule times
    for rule in rules:
        start_dt = timezone.make_aware(datetime.combine(now.date(), rule.start_time), now.tzinfo)
        end_dt   = timezone.make_aware(datetime.combine(now.date(), rule.end_time),   now.tzinfo)

        window_start = start_dt - timedelta(minutes=window_minutes)
        window_end   = end_dt + timedelta(minutes=window_minutes)

        if window_start <= now <= window_end:
            return True

    return False

# ---- your original functions, adapted ----

def create_skyroom_link(user: User, course: Course) -> str | None:
    """
    Returns the Skyroom login URL if the user is authorized AND within the shift window.
    Otherwise returns None (caller can decide to 400).
    """
    # 1) access check via parent/child relationships
    if not _user_has_access_to_course(user, course):
        return None

    # 2) timing check against today's shift
    if not _now_in_shift_window(course, window_minutes=15):
        return None

    # 3) generate link
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
        ttl: int = 5400):
    url = f"{SKYROOM_BASEURL}/skyroom/api/{SKYROOM_APIKEY}"
    payload = {
        "action": "createLoginUrl",
        "params": {
            "room_id": room_id,
            "user_id": user_id,
            "nickname": nickname,
            "access": 1,      # Access level
            "concurrent": 1,
            "language": language,
            "ttl": ttl,
        },
    }
    r = requests.post(url, json=payload, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json().get("result", None)
