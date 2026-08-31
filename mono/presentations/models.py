"""Purchasable ICPC 2026 offerings and their registrations.

The current catalogue has four individual offering types: online presentations
(250 seats, 85,000 Toman), offline presentations (unlimited, 60,000 Toman),
in-person workshops (125 seats, 125,000 Toman), and online workshops (80 seats,
85,000 Toman). The price and capacity are stored on each ``Course`` so the
database remains the source of truth; the type-specific values are applied as
creation defaults.

Full packages are intentionally not offered in this release. The legacy
``children`` relationship and ``RegistrationItem`` rows remain only to preserve
last year's data and access. If packages return, they must not have their own
capacity: finalizing one purchase must atomically claim every finite child seat.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q
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
        help_text="Legacy bundle composition; new package purchases are disabled.",
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
        defaults = OFFERING_DEFAULTS.get(self.offering_type)
        if not defaults:
            return
        self.online = defaults["online"]
        self.onsite = defaults["onsite"]
        if self.price is None:
            self.price = defaults["price"]
        if self.offering_type == self.OfferingType.OFFLINE_PRESENTATION:
            self.capacity = None
        elif self.capacity is None:
            self.capacity = defaults["capacity"]

    def save(self, *args, **kwargs):
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

                    promote_waitlist_task.delay(course_id)

                transaction.on_commit(promote_after_capacity_change, robust=True)
            return result

    @property
    def is_unlimited(self) -> bool:
        return self.capacity is None

    def remained_capacity(self) -> int | None:
        """Remaining seats considering both direct and child purchases."""
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

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="registrations")
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

    # set by backoffice on approve; payment app will generate proper link later
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
    """How many seats of `course` are occupied in COUNT_STATUSES,
    counting both direct (parent) and child purchases."""
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
        direct = len(
            list(direct_qs.select_for_update().values_list("id", flat=True))
        )
        children = len(
            list(child_qs.select_for_update().values_list("id", flat=True))
        )
        return direct + children
    return direct_qs.count() + child_qs.count()

def _is_full_by_count(course) -> bool:
    """True if course capacity is exhausted according to COUNT_STATUSES."""
    cap = course.capacity
    if cap is None:
        return False
    if cap == 0:
        return True
    return _taken_seats(course) >= cap
