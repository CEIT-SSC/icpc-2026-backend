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
    _is_full_by_count,
    _taken_seats,
)
from notification.services import send_status_change_email
from accounts.models import UserExtraData
from django.conf import settings

User = get_user_model()

SKYROOM_BASEURL = settings.SKYROOM_BASEURL
SKYROOM_APIKEY = settings.SKYROOM_APIKEY
SKYROOM_ROOMID = settings.SKYROOM_ROOMID
HEADERS = {"accept": "application/json", "content-type": "application/json"}


def _parent_capacity_full(course: Course) -> bool:
    return _is_full_by_count(course)


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


def _lock_and_validate_capacity(registrations: list[Registration]) -> None:
    """Lock every claimed offering and validate the batch as one atomic claim."""
    if not registrations:
        return

    claims = _capacity_claims(registrations)
    courses = {
        course.id: course
        for course in Course.objects.select_for_update()
        .filter(id__in=sorted(claims))
        .order_by("id")
    }
    excluded_ids = [reg.id for reg in registrations]

    for course_id, requested_seats in claims.items():
        course = courses[course_id]
        if course.capacity is None:
            continue
        occupied = _taken_seats(
            course, exclude_registration_ids=excluded_ids
        )
        if occupied + requested_seats > course.capacity:
            raise CustomAPIException(
                code=EC.REG_CAPACITY_UNAVAILABLE,
                message=f"No remaining capacity for {course.name}.",
                status_code=status.HTTP_409_CONFLICT,
            )


@transaction.atomic
def submit_registration(
    *,
    course: Course,
    user: User,
    extra_updates: dict | None = None,
    child_ids: list[int] | None = None,
    resume_url: str | None = None,
) -> Registration:
    """
    Do not create a payment when full; preserve the existing RESERVED waitlist.
    If the individual offering has a seat, set QUEUED and follow its configured
    approval/payment flow.
    If no approval required AND status is QUEUED => auto-approve/finalize (free) or create payment link.

    Additionally: prevent buying a course/child that the user already owns (FINAL),
    even if ownership came through a different parent registration.
    """
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

    parent_full = _parent_capacity_full(course)
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
    if reg is None:
        reg = Registration(course=course, user=user)

    reg.resume_url = resume_url or reg.resume_url
    reg.price = course.price
    reg.submitted_at = timezone.now()
    reg.rejection_reason = ""
    reg.status = Registration.Status.RESERVED if parent_full else Registration.Status.QUEUED
    reg.save()

    if extra_updates:
        extra, _ = UserExtraData.objects.get_or_create(user=user)
        extra.answers = {**(extra.answers or {}), **extra_updates}
        if "codeforces_score" in extra_updates:
            try:
                extra.codeforces_score = int(extra_updates["codeforces_score"])
            except Exception:
                pass
        if "codeforces_handle" in extra_updates:
            extra.codeforces_handle = str(extra_updates["codeforces_handle"])[:64]
        extra.save()

    RegistrationItem.objects.filter(registration=reg).delete()

    send_status_change_email(
        to=user.email,
        status_code="COURSE_REQUEST_SUBMITTED",
        extra={
            "course": course.name,
            "status": reg.status,
            "waitlisted_children": "",
        },
    )

    requires_approval = course.requires_approval or parent_full

    if not requires_approval and reg.status == Registration.Status.QUEUED:
        reg = _auto_progress_to_payment(reg)

    return reg

@transaction.atomic
def set_status_approved(
    reg: Registration,
    *,
    actor: User | None = None,
    payment_link: str | None = None,
    override_amount: int | None = None,
    description: str | None = None,
) -> Registration:
    reg = (
        Registration.objects.select_for_update()
        .select_related("course", "user")
        .get(pk=reg.pk)
    )
    _validate_individual_offering(reg.course, [])
    if reg.items.exists():
        raise CustomAPIException(
            code=EC.REG_PACKAGE_UNAVAILABLE,
            message="Package purchases are not available in the current offering catalogue.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if reg.status in (Registration.Status.APPROVED, Registration.Status.FINAL):
        raise CustomAPIException(
            code=EC.REG_ALREADY_FINAL_OR_APPROVED,
            message="This registration is already approved or finalized.",
            status_code=status.HTTP_409_CONFLICT,
        )

    _lock_and_validate_capacity([reg])
    reg.status = Registration.Status.APPROVED
    reg.save(update_fields=["status"])

    if payment_link is None:
        amount = override_amount if override_amount is not None else _compute_total_amount(reg)

        meta = {
            "reg_id": reg.id,
            "course_id": reg.course_id,
        }

        payment_result = initiate_payment_for_target(
            user=reg.user,
            target_type=Payment.TargetType.COURSE,
            target_id=str(reg.id),
            amount=amount,
            description=description or _compose_description(reg),
            extra_metadata=meta,
        )
        reg.payment_link = payment_result.url
    else:
        reg.payment_link = payment_link

    reg.decided_at = timezone.now()
    reg.save(update_fields=["status", "payment_link", "decided_at"])

    send_status_change_email(
        to=reg.user.email,
        status_code="COURSE_REQUEST_APPROVED",
        extra={"course": reg.course.name, "payment_link": reg.payment_link},
    )
    return reg


@transaction.atomic
def set_status_final(regs: list[Registration], *, actor: User | None = None) -> list[Registration]:
    if isinstance(regs, Registration):
        regs = [regs]
    reg_ids = [reg.id for reg in regs]
    if not reg_ids:
        return []

    locked_by_id = {
        reg.id: reg
        for reg in Registration.objects.select_for_update()
        .select_related("course", "user")
        .filter(id__in=reg_ids)
    }
    regs = [locked_by_id[reg_id] for reg_id in reg_ids]
    _lock_and_validate_capacity(regs)

    for reg in regs:
        reg.status = Registration.Status.FINAL
        reg.decided_at = timezone.now()
        reg.save(update_fields=["status", "decided_at"])

    for reg in regs:
        send_status_change_email(
            to=reg.user.email,
            status_code="COURSE_REQUEST_FINAL",
            extra={"course": reg.course.name},
        )
    return regs


@transaction.atomic
def cancel_registration_for_failed_payment(registration_id: int, *, user: User) -> None:
    """Release an APPROVED seat when its gateway payment definitively fails."""
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


@transaction.atomic
def set_status_rejected(reg: Registration, *, actor: User | None = None) -> Registration:
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
    reg.status = Registration.Status.REJECTED
    reg.decided_at = timezone.now()
    reg.save(update_fields=["status", "decided_at"])
    send_status_change_email(
        to=reg.user.email,
        status_code="COURSE_REQUEST_REJECTED",
        extra={"course": reg.course.name, "reason": reg.rejection_reason},
    )
    return reg


def _auto_progress_to_payment(reg: Registration) -> Registration:
    total = _compute_total_amount(reg)
    if total <= 0:
        return set_status_final([reg])[0]
    return set_status_approved(
        reg,
        override_amount=total,
        description=_compose_description(reg),
    )

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
