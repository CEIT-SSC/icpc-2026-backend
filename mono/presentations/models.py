"""ICPC 2026 bundle catalogue and preservation-safe registration history."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q
from django.db.models.signals import m2m_changed
from django.dispatch import receiver
from django.utils import timezone
from django.utils.text import slugify

User = settings.AUTH_USER_MODEL


OFFERING_DEFAULTS = {
    "ONLINE_PRESENTATION": {
        "capacity": 250,
        "price": 85_000,
        "online": True,
        "onsite": False,
    },
    "OFFLINE_PRESENTATION": {
        "capacity": None,
        "price": 60_000,
        "online": False,
        "onsite": False,
    },
    "IN_PERSON_WORKSHOP": {
        "capacity": 125,
        "price": 125_000,
        "online": False,
        "onsite": True,
    },
    "ONLINE_WORKSHOP": {
        "capacity": 80,
        "price": 85_000,
        "online": True,
        "onsite": False,
    },
}

BUNDLE_CATALOG = {
    "ALL_ONLINE_PRESENTATIONS": {
        "name": "All online presentations",
        "slug": "online-presentations-bundle",
        "price": 599_000,
        "offering_type": "ONLINE_PRESENTATION",
        "expected_member_count": 6,
        "category": "PRESENTATION",
        "delivery_mode": "ONLINE",
        "online": True,
        "onsite": False,
    },
    "ALL_IN_PERSON_WORKSHOPS": {
        "name": "All in-person workshops",
        "slug": "in-person-workshops-bundle",
        "price": 419_000,
        "offering_type": "IN_PERSON_WORKSHOP",
        "expected_member_count": 3,
        "category": "WORKSHOP",
        "delivery_mode": "IN_PERSON",
        "online": False,
        "onsite": True,
    },
    "ALL_ONLINE_WORKSHOPS": {
        "name": "All online workshops",
        "slug": "online-workshops-bundle",
        "price": 299_000,
        "offering_type": "ONLINE_WORKSHOP",
        "expected_member_count": 3,
        "category": "WORKSHOP",
        "delivery_mode": "ONLINE",
        "online": True,
        "onsite": False,
    },
}




class Presenter(models.Model):
    full_name = models.CharField(max_length=120)
    bio = models.TextField(blank=True)
    email = models.EmailField(blank=True)
    website = models.URLField(blank=True)

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name


class Course(models.Model):
    class OfferingType(models.TextChoices):
        ONLINE_PRESENTATION = "ONLINE_PRESENTATION", "Online presentation"
        OFFLINE_PRESENTATION = "OFFLINE_PRESENTATION", "Offline presentation"
        IN_PERSON_WORKSHOP = "IN_PERSON_WORKSHOP", "In-person workshop"
        ONLINE_WORKSHOP = "ONLINE_WORKSHOP", "Online workshop"

    class BundleType(models.TextChoices):
        ALL_ONLINE_PRESENTATIONS = (
            "ALL_ONLINE_PRESENTATIONS",
            "All online presentations",
        )
        ALL_IN_PERSON_WORKSHOPS = (
            "ALL_IN_PERSON_WORKSHOPS",
            "All in-person workshops",
        )
        ALL_ONLINE_WORKSHOPS = (
            "ALL_ONLINE_WORKSHOPS",
            "All online workshops",
        )

    name = models.CharField(max_length=200)
    subtitle = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    presenters = models.ManyToManyField(Presenter, related_name="courses", blank=True)

    start_date = models.DateField(null=True, blank=True)
    online = models.BooleanField(default=True)
    onsite = models.BooleanField(default=False)
    classes_count = models.PositiveIntegerField(default=0)

    offering_type = models.CharField(
        max_length=24,
        choices=OfferingType.choices,
        null=True,
        blank=True,
        db_index=True,
        help_text="Blank values are retained only for legacy offerings.",
    )
    bundle_type = models.CharField(
        max_length=32,
        choices=BundleType.choices,
        null=True,
        blank=True,
        db_index=True,
        help_text="Stable product type for current all-access bundle parents.",
    )
    capacity = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=None,
        help_text="Leave blank for unlimited capacity.",
    )
    price = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text="Price in Toman (IRT).",
    )

    children = models.ManyToManyField(
        "self",
        symmetrical=False,
        related_name="parents",
        blank=True,
        help_text=(
            "Current bundle composition. RegistrationItem preserves the purchased "
            "composition historically."
        ),
    )

    requires_approval = models.BooleanField(
        default=False,
        help_text="Legacy setting; registrations now progress automatically when capacity is available.",
    )

    slug = models.SlugField(max_length=220, unique=True, blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def apply_offering_defaults(self) -> None:
        if self.bundle_type:
            return
        defaults = OFFERING_DEFAULTS.get(self.offering_type)
        if not defaults:
            return
        self.online = defaults["online"]
        self.onsite = defaults["onsite"]
        if self.price is None and self._state.adding:
            self.price = defaults["price"]
        if self.offering_type == self.OfferingType.OFFLINE_PRESENTATION:
            self.capacity = None
        elif self.capacity is None and self._state.adding:
            self.capacity = defaults["capacity"]

    def save(self, *args, **kwargs):
        if self.bundle_type in BUNDLE_CATALOG:
            config = BUNDLE_CATALOG[self.bundle_type]
            self.offering_type = None
            self.capacity = None
            self.online = config["online"]
            self.onsite = config["onsite"]
            if not self.slug:
                self.slug = config["slug"]
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                    "offering_type",
                    "capacity",
                    "online",
                    "onsite",
                    "slug",
                }
        self.apply_offering_defaults()
        if not self.slug:
            self.slug = slugify(self.name)[:220]

        update_fields = kwargs.get("update_fields")
        capacity_is_being_saved = update_fields is None or "capacity" in update_fields
        if self._state.adding or not capacity_is_being_saved:
            return super().save(*args, **kwargs)

        # Every seat allocation also locks this row first. Locking capacity
        # changes here makes increases/decreases serialize with registrations,
        # payments, cancellations, and waitlist promotions.
        with transaction.atomic():
            current = Course.objects.select_for_update().get(pk=self.pk)
            capacity_changed = current.capacity != self.capacity
            if capacity_changed and self.capacity is not None:
                occupied = _taken_seats(current, for_update=True)
                if self.capacity < occupied:
                    raise ValidationError(
                        {
                            "capacity": (
                                f"Capacity cannot be lower than the {occupied} "
                                "currently allocated seat(s)."
                            )
                        }
                    )

            result = super().save(*args, **kwargs)
            if capacity_changed:
                course_id = self.pk

                def promote_after_capacity_change():
                    from .tasks import promote_waitlist_task

                    promote_waitlist_task.delay([course_id])

                transaction.on_commit(promote_after_capacity_change, robust=True)
            return result

    def clean(self):
        super().clean()
        if self.bundle_type:
            errors = {}
            config = BUNDLE_CATALOG.get(self.bundle_type)
            if not config:
                errors["bundle_type"] = "Unknown bundle type."
            if self.offering_type is not None:
                errors["offering_type"] = (
                    "A typed bundle parent cannot also be a component offering."
                )
            if self.capacity is not None:
                errors["capacity"] = (
                    "Bundle capacity is derived from members and must be blank."
                )
            if self.price is None:
                errors["price"] = "A typed bundle must have a bundle price."
            if config and self.slug and self.slug != config["slug"]:
                errors["slug"] = "Typed bundles must use their stable catalogue slug."
            if errors:
                raise ValidationError(errors)

    @property
    def is_bundle(self) -> bool:
        return self.bundle_type in Course.BundleType.values

    @property
    def category(self) -> str | None:
        if self.is_bundle:
            return BUNDLE_CATALOG[self.bundle_type]["category"]
        if self.offering_type in (
            self.OfferingType.ONLINE_PRESENTATION,
            self.OfferingType.OFFLINE_PRESENTATION,
        ):
            return "PRESENTATION"
        if self.offering_type in (
            self.OfferingType.IN_PERSON_WORKSHOP,
            self.OfferingType.ONLINE_WORKSHOP,
        ):
            return "WORKSHOP"
        return None

    @property
    def delivery_mode(self) -> str | None:
        if self.is_bundle:
            return BUNDLE_CATALOG[self.bundle_type]["delivery_mode"]
        if self.offering_type in (
            self.OfferingType.ONLINE_PRESENTATION,
            self.OfferingType.ONLINE_WORKSHOP,
        ):
            return "ONLINE"
        if self.offering_type == self.OfferingType.IN_PERSON_WORKSHOP:
            return "IN_PERSON"
        return None

    def bundle_members(self):
        cached = getattr(self, "_prefetched_objects_cache", {}).get("children")
        if cached is not None:
            return sorted(cached, key=lambda member: member.id)
        return self.children.all().order_by("id")

    def bundle_composition_errors(self, members=None) -> list[str]:
        """Return current-bundle errors without mutating legacy relationships."""
        if not self.is_bundle:
            return ["Course is not a typed bundle."]
        config = BUNDLE_CATALOG[self.bundle_type]
        members = list(self.bundle_members() if members is None else members)
        errors = []
        if len(members) != config["expected_member_count"]:
            errors.append(
                f"{self.get_bundle_type_display()} requires exactly "
                f"{config['expected_member_count']} members."
            )
        expected_type = config["offering_type"]
        invalid = [
            member
            for member in members
            if member.bundle_type
            or member.offering_type != expected_type
            or not member.is_active
        ]
        if invalid:
            errors.append(
                "Every member must be an active component with offering type "
                f"{expected_type}."
            )
        for member in members:
            other_parents = [
                parent
                for parent in member.parents.all()
                if parent.pk != self.pk and parent.is_active and parent.is_bundle
            ]
            if other_parents:
                errors.append(
                    f"{member.name} already belongs to another active typed bundle."
                )
                break
        return errors

    def is_valid_bundle(self) -> bool:
        if (
            not self.is_bundle
            or not self.is_active
            or self.offering_type is not None
            or self.capacity is not None
            or self.price is None
            or self.slug != BUNDLE_CATALOG[self.bundle_type]["slug"]
        ):
            return False
        return not self.bundle_composition_errors()

    def effective_capacity(self) -> int | None:
        if not self.is_bundle:
            return self.capacity
        members = list(self.bundle_members())
        finite = [member.capacity for member in members if member.capacity is not None]
        return min(finite) if finite else None

    @property
    def is_unlimited(self) -> bool:
        if self.is_bundle:
            members = list(self.bundle_members())
            return bool(members) and all(member.capacity is None for member in members)
        return self.capacity is None

    def remained_capacity(self) -> int | None:
        """Return a component's seats or a bundle's all-members bottleneck."""
        if self.is_bundle:
            members = list(self.bundle_members())
            finite_remaining = [
                member.remained_capacity()
                for member in members
                if member.capacity is not None
            ]
            return min(finite_remaining) if finite_remaining else None
        if self.is_unlimited:
            return None
        used = _taken_seats(self)
        return max(self.capacity - used, 0)

    def __str__(self):
        return self.name


class ScheduleRule(models.Model):
    class Weekday(models.IntegerChoices):
        MON = 0, "Mon"
        TUE = 1, "Tue"
        WED = 2, "Wed"
        THU = 3, "Thu"
        FRI = 4, "Fri"
        SAT = 5, "Sat"
        SUN = 6, "Sun"

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="schedule")
    weekday = models.IntegerField(choices=Weekday.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["weekday", "start_time"]
        unique_together = ("course", "weekday", "start_time", "end_time")

    def __str__(self):
        return f"{self.get_weekday_display()} {self.start_time}-{self.end_time}"


class Registration(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = "SUBMITTED", "Submitted"
        RESERVED = "RESERVED", "Waitlisted"  # capacity full -> FIFO waitlist
        QUEUED = "QUEUED", "Queued"         # capacity available -> QUEUED
        APPROVED = "APPROVED", "Approved"
        FINAL = "FINAL", "Finalized"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="registrations")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="course_registrations")
    price = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text="Price snapshot in Toman (IRT).",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SUBMITTED)

    # optional resume link or blob pointer
    resume_url = models.URLField(blank=True)

    # set by backoffice on reject
    rejection_reason = models.TextField(blank=True)

    # Populated only after the owner explicitly starts payment.
    payment_link = models.URLField(blank=True)

    submitted_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("course", "user")
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(
                fields=["course", "status", "submitted_at", "id"],
                name="pres_reg_wait_fifo_idx",
            )
        ]

    def __str__(self):
        return f"Reg<{self.user_id}:{self.course.slug}:{self.status}>"

    def waitlist_position(self) -> int | None:
        """Return the current one-based FIFO position for a waitlisted user."""
        if self.status != self.Status.RESERVED or not self.pk:
            return None
        snapshot = getattr(self, "_waitlist_position_snapshot", None)
        if snapshot is not None:
            return snapshot
        earlier = Registration.objects.filter(
            course_id=self.course_id,
            status=self.Status.RESERVED,
        ).filter(
            Q(submitted_at__lt=self.submitted_at)
            | Q(submitted_at=self.submitted_at, id__lt=self.id)
        )
        return earlier.count() + 1


class RegistrationItem(models.Model):
    """
    Child presentation selection for a given registration.
    """
    registration = models.ForeignKey(
        Registration, on_delete=models.CASCADE, related_name="items"
    )
    child_course = models.ForeignKey(
        Course, on_delete=models.PROTECT, related_name="registration_items"
    )
    price = models.IntegerField(validators=[MinValueValidator(0)])
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("registration", "child_course")

    def __str__(self):
        return f"RegItem<{self.registration_id}:{self.child_course.slug}:{self.price}>"

class CourseSession(models.Model):
    title = models.CharField(max_length=200, blank=True)
    subtitle = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="sessions")
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_online = models.BooleanField(default=True)
    is_onsite = models.BooleanField(default=False)
    recording_link = models.URLField(blank=True)


COUNT_STATUSES = (Registration.Status.APPROVED, Registration.Status.FINAL)

def _taken_seats(
    course, *, exclude_registration_ids=None, for_update: bool = False
) -> int:
    """Count distinct participants across direct and snapshotted claims."""
    if not exclude_registration_ids and not for_update:
        snapshot = getattr(course, "_taken_seats_snapshot", None)
        if snapshot is not None:
            return snapshot
    direct_qs = Registration.objects.filter(
        course=course,
        status__in=COUNT_STATUSES,
    )
    child_qs = RegistrationItem.objects.filter(
        child_course=course,
        registration__status__in=COUNT_STATUSES,
    )
    if exclude_registration_ids:
        direct_qs = direct_qs.exclude(id__in=exclude_registration_ids)
        child_qs = child_qs.exclude(registration_id__in=exclude_registration_ids)
    if for_update:
        # A locking read is a current read on MySQL. This avoids an older
        # REPEATABLE READ snapshot after waiting for the course lock.
        direct_users = set(
            direct_qs.select_for_update().values_list("user_id", flat=True)
        )
        child_users = set(
            child_qs.select_for_update().values_list(
                "registration__user_id", flat=True
            )
        )
        return len(direct_users | child_users)
    direct_users = set(direct_qs.values_list("user_id", flat=True))
    child_users = set(
        child_qs.values_list("registration__user_id", flat=True)
    )
    return len(direct_users | child_users)


def attach_capacity_snapshots(courses) -> None:
    """Attach bulk participant counts to courses used by public serializers."""
    roots = list(courses)
    by_id = {course.id: course for course in roots}
    for course in roots:
        if course.is_bundle:
            for member in course.bundle_members():
                by_id[member.id] = member
    ids = list(by_id)
    participants = {course_id: set() for course_id in ids}
    for course_id, user_id in Registration.objects.filter(
        course_id__in=ids,
        status__in=COUNT_STATUSES,
    ).values_list("course_id", "user_id"):
        participants[course_id].add(user_id)
    for course_id, user_id in RegistrationItem.objects.filter(
        child_course_id__in=ids,
        registration__status__in=COUNT_STATUSES,
    ).values_list("child_course_id", "registration__user_id"):
        participants[course_id].add(user_id)
    for course_id, course in by_id.items():
        course._taken_seats_snapshot = len(participants[course_id])


@receiver(m2m_changed, sender=Course.children.through)
def validate_typed_bundle_member_addition(
    sender, instance, action, reverse, model, pk_set, **kwargs
):
    """Block invalid/overlapping additions while ignoring legacy relationships."""
    if action in ("pre_remove", "pre_clear"):
        if reverse:
            parent_ids = set(pk_set or instance.parents.values_list("id", flat=True))
            lock_ids = {instance.pk, *parent_ids}
        elif instance.is_bundle:
            member_ids = set(pk_set or instance.children.values_list("id", flat=True))
            lock_ids = {instance.pk, *member_ids}
        else:
            return
        list(
            Course.objects.select_for_update()
            .filter(pk__in=sorted(lock_ids))
            .order_by("id")
        )
        return
    if action != "pre_add" or not pk_set:
        return
    if reverse:
        list(
            Course.objects.select_for_update()
            .filter(pk__in=sorted({instance.pk, *pk_set}))
            .order_by("id")
        )
        parents = list(
            Course.objects.filter(
                pk__in=pk_set,
                is_active=True,
                bundle_type__isnull=False,
            )
        )
        if not parents:
            return
        if any(
            instance.bundle_type
            or instance.offering_type
            != BUNDLE_CATALOG[parent.bundle_type]["offering_type"]
            or not instance.is_active
            for parent in parents
        ):
            raise ValidationError(
                "Typed bundles may contain only active components of their configured type."
            )
        existing = instance.parents.filter(
            is_active=True,
            bundle_type__isnull=False,
        ).exclude(pk__in=pk_set)
        if existing.exists() or len(parents) > 1:
            raise ValidationError(
                "A component cannot belong to more than one active typed bundle."
            )
        return
    if not instance.is_bundle:
        return
    config = BUNDLE_CATALOG[instance.bundle_type]
    locked = list(
        Course.objects.select_for_update()
        .filter(pk__in=sorted({instance.pk, *pk_set}))
        .order_by("id")
    )
    members = [course for course in locked if course.pk in pk_set]
    if len(members) != len(pk_set) or any(
        member.bundle_type
        or member.offering_type != config["offering_type"]
        or not member.is_active
        for member in members
    ):
        raise ValidationError(
            "Typed bundles may contain only active components of their configured type."
        )
    overlapping = Course.objects.filter(
        children__id__in=pk_set,
        is_active=True,
        bundle_type__isnull=False,
    ).exclude(pk=instance.pk)
    if overlapping.exists():
        raise ValidationError(
            "A component cannot belong to more than one active typed bundle."
        )

def _is_full_by_count(course) -> bool:
    """True if course capacity is exhausted according to COUNT_STATUSES."""
    cap = course.capacity
    if cap is None:
        return False
    if cap == 0:
        return True
    return _taken_seats(course) >= cap


class DiscountCode(models.Model):
    code = models.CharField(max_length=32, unique=True)
    percent_off = models.PositiveIntegerField(null=True, blank=True)  # the prcentage discount 
    amount_off = models.PositiveIntegerField(null=True, blank=True)   # mizanesh
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    used_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    # اختیاری: محدود به یک دوره خاص
    course = models.ForeignKey(Course, null=True, blank=True, on_delete=models.CASCADE, related_name="discount_codes")

    def is_valid(self):
        now = timezone.now()
        if not self.is_active:
            return False
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_until and now > self.valid_until:
            return False
        if self.max_uses is not None and self.used_count >= self.max_uses:
            return False
        return True

    def apply(self, price: int) -> int:
        if self.percent_off:
            return max(0, price - (price * self.percent_off // 100))
        if self.amount_off:
            return max(0, price - self.amount_off)
        return price