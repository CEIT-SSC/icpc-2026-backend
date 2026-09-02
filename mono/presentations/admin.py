from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError

from .models import (
    BUNDLE_CATALOG,
    Course,
    CourseSession,
    DiscountCode,
    Presenter,
    Registration,
    ScheduleRule,
)
from .services import set_status_approved, set_status_rejected, set_status_final




class ScheduleInline(admin.TabularInline):
    model = ScheduleRule
    extra = 0


class CourseAdminForm(forms.ModelForm):
    class Meta:
        model = Course
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        bundle_type = cleaned.get("bundle_type")
        if not bundle_type:
            return cleaned
        members = list(cleaned.get("children") or [])
        config = BUNDLE_CATALOG[bundle_type]
        errors = []
        if len(members) != config["expected_member_count"]:
            errors.append(
                f"Select exactly {config['expected_member_count']} bundle members."
            )
        if any(
            member.bundle_type
            or member.offering_type != config["offering_type"]
            or not member.is_active
            for member in members
        ):
            errors.append(
                "Members must be active component courses of the configured offering type."
            )
        overlapping = Course.objects.filter(
            children__in=members,
            is_active=True,
            bundle_type__isnull=False,
        )
        if self.instance.pk:
            overlapping = overlapping.exclude(pk=self.instance.pk)
        if overlapping.exists():
            errors.append(
                "A selected component already belongs to another active typed bundle."
            )
        if errors:
            raise ValidationError({"children": errors})
        return cleaned




@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    form = CourseAdminForm
    list_display = (
        "name",
        "bundle_type",
        "offering_type",
        "effective_capacity_display",
        "remaining_capacity",
        "price",
        "is_active",
    )
    list_filter = ("bundle_type", "offering_type", "is_active")
    search_fields = ("name", "subtitle", "slug")
    inlines = [ScheduleInline]
    filter_horizontal = ("presenters", "children")
    readonly_fields = ("online", "onsite", "requires_approval")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and obj.is_bundle:
            fields.append("capacity")
        return tuple(fields)

    @admin.display(description="Remaining capacity")
    def remaining_capacity(self, obj: Course):
        remaining = obj.remained_capacity()
        return "Unlimited" if remaining is None else remaining

    @admin.display(description="Effective capacity")
    def effective_capacity_display(self, obj: Course):
        capacity = obj.effective_capacity()
        return "Unlimited" if capacity is None else capacity


@admin.register(CourseSession)
class CourseSessionAdmin(admin.ModelAdmin):
    list_display = ("course", "start_time", "end_time")
    search_fields = ("course__name", "course__subtitle")
    list_filter = ("course",)


@admin.register(DiscountCode)
class DiscountCodeAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "course",
        "percent_off",
        "amount_off",
        "used_count",
        "max_uses",
        "is_active",
        "valid_from",
        "valid_until",
    )
    list_filter = ("is_active", "course")
    search_fields = ("code", "course__name", "course__slug")
    readonly_fields = ("used_count",)



@admin.register(Presenter)
class PresenterAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "website")
    search_fields = ("full_name", "email")




@admin.register(Registration)
class RegistrationAdmin(admin.ModelAdmin):
    """
    Enriched Registration admin with user's full data in list & detail views.
    """
    
    list_display = (
        "id",
        "course",
        "bundle_type",
        "user_email",
        "user_full_name",
        "user_phone",
        "price",
        "status",
        "submitted_at",
        "member_snapshot",
        "effective_remaining_capacity",
        "decided_at",
        "discount_code",
    )
    list_select_related = ("user", "course")
    list_filter = ("status", "course__bundle_type", "course")
    search_fields = (
        "course__name",
        "course__slug",
        "user__email",
        "user__first_name",
        "user__last_name",
        "user__phone_number",
    )
    ordering = ("-submitted_at",)
    date_hierarchy = "submitted_at"

    
    raw_id_fields = ("user", "course")

    
    readonly_fields = (
        "user_email",
        "user_first_name",
        "user_last_name",
        "user_phone",
        "price",
        "discount_code",
        "submitted_at",
        "decided_at",
        "payment_link",
        "member_snapshot",
        "effective_remaining_capacity",
    )

    
    fieldsets = (
        ("Registration", {
            "fields": (
                "course",
                "user",
                "price",
                "discount_code",
                "member_snapshot",
                "effective_remaining_capacity",
                "status",
                "payment_link",
                "rejection_reason",
            )
        }),
        ("Timestamps", {
            "fields": ("submitted_at", "decided_at"),
        }),
        ("User (read-only)", {
            "fields": (
                "user_email",
                "user_first_name",
                "user_last_name",
                "user_phone",
            )
        }),
    )

    
    actions = ("approve_selected", "reject_selected", "finalize_selected")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("course", "user").prefetch_related(
            "items__child_course", "course__children"
        )

    @admin.display(ordering="course__bundle_type", description="Bundle type")
    def bundle_type(self, obj: Registration) -> str:
        return obj.course.bundle_type or "Legacy / individual"

    @admin.display(description="Member snapshot")
    def member_snapshot(self, obj: Registration) -> str:
        names = [item.child_course.name for item in obj.items.all()]
        return ", ".join(names) if names else "—"

    @admin.display(description="Effective remaining capacity")
    def effective_remaining_capacity(self, obj: Registration):
        remaining = obj.course.remained_capacity()
        return "Unlimited" if remaining is None else remaining

    

    @admin.display(ordering="user__email", description="User email")
    def user_email(self, obj: Registration) -> str:
        return getattr(obj.user, "email", "") or ""

    @admin.display(description="User name")
    def user_full_name(self, obj: Registration) -> str:
        fn = (getattr(obj.user, "first_name", "") or "").strip()
        ln = (getattr(obj.user, "last_name", "") or "").strip()
        return f"{fn} {ln}".strip() or "(no name)"

    @admin.display(description="First name")
    def user_first_name(self, obj: Registration) -> str:
        return getattr(obj.user, "first_name", "") or ""

    @admin.display(description="Last name")
    def user_last_name(self, obj: Registration) -> str:
        return getattr(obj.user, "last_name", "") or ""

    @admin.display(ordering="user__phone_number", description="Phone")
    def user_phone(self, obj: Registration) -> str:
        return getattr(obj.user, "phone_number", "") or ""

    

    def approve_selected(self, request, queryset):
        count = 0
        for reg in queryset.select_related("course", "user"):
            set_status_approved(reg, actor=request.user)
            count += 1
        self.message_user(request, f"Approved {count} registration(s)")
    approve_selected.short_description = (
        "Approve (payment link is created when the user chooses to pay)"
    )



    def reject_selected(self, request, queryset):
        count = 0
        for reg in queryset.select_related("course", "user"):
            
            if not reg.rejection_reason:
                continue
            set_status_rejected(reg, actor=request.user)
            count += 1
        self.message_user(request, f"Rejected {count} registration(s)")
    reject_selected.short_description = "Reject (requires rejection_reason)"

    def finalize_selected(self, request, queryset):
        regs = list(
            queryset.filter(status=Registration.Status.APPROVED).select_related(
                "course", "user"
            )
        )
        set_status_final(regs, actor=request.user)
        count = len(regs)
        self.message_user(request, f"Marked {count} registration(s) as paid")
    finalize_selected.short_description = "Mark paid (FINAL)"
