from django.contrib import admin
from .models import Competition, CompetitionFieldConfig, TeamRequest, TeamMember
from .services import (
    backoffice_approve_request, backoffice_reject_request,
    mark_payment_final, cancel_request,
)
from django.db.models import (
    Count, F, Q, Value, Case, When, CharField, Prefetch, Aggregate, Func
)
from django.db.models.functions import Concat, Coalesce

class FieldConfigInline(admin.StackedInline):
    model = CompetitionFieldConfig
    extra = 0

@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "min_team_size", "max_team_size", "signup_fee_aut", "signup_fee_base", "requires_backoffice_approval", "is_active")
    search_fields = ("name", "slug")
    inlines = [FieldConfigInline]

class TeamMemberInline(admin.TabularInline):
    model = TeamMember
    extra = 0

class GroupConcat(Aggregate):
    function = "GROUP_CONCAT"
    output_field = CharField()
    allow_distinct = True

    template = "%(function)s(%(distinct)s%(expressions)s%(ordering)s%(separator)s)"

    def __init__(self, expression, distinct=False, separator=", ", ordering=None, **extra):
        ordering_sql = f" ORDER BY {ordering}" if ordering else ""
        separator_sql = f" SEPARATOR '{separator}'" if separator is not None else ""
        super().__init__(
            expression,
            distinct=distinct,
            ordering=ordering_sql,
            separator=separator_sql,
            **extra,
        )

class UnivUniquenessFilter(admin.SimpleListFilter):
    title = "university names unique?"
    parameter_name = "univ_unique"

    def lookups(self, request, model_admin):
        return (("yes", "Yes — all same/empty"), ("no", "No — multiple distinct"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(university_names_distinct_count__lte=1)
        if self.value() == "no":
            return queryset.filter(university_names_distinct_count__gt=1)
        return queryset


@admin.register(TeamRequest)
class TeamRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "competition",
        "team_name",
        "submitter",
        "status",
        "university_names",
        "are_university_names_unique",
        "member_names",
        "created_at",
    )
    list_filter = ("status", "competition", UnivUniquenessFilter)
    search_fields = (
        "team_name",
        "submitter__email",
        "members__first_name",
        "members__last_name",
        "members__email",
        "members__university_name",
    )
    inlines = [TeamMemberInline]
    list_select_related = ("competition", "submitter")

    actions = ["approve_selected", "reject_selected", "mark_final_selected"]

    
    def get_queryset(self, request):
        qs = (
            super()
            .get_queryset(request)
            .select_related("competition", "submitter")
            .prefetch_related(
                Prefetch(
                    "members",
                    queryset=TeamMember.objects.only(
                        "first_name", "last_name", "university_name", "request_id"
                    ),
                )
            )
        )

        
        nonblank_university = Case(
            When(members__university_name__gt="", then=F("members__university_name")),
            default=Value(None),
            output_field=CharField(),
        )

        
        full_name = Concat(
            Coalesce(F("members__first_name"), Value("")),
            Value(" "),
            Coalesce(F("members__last_name"), Value("")),
        )
        nonblank_full_name = Case(
            When(~(Q(members__first_name="") & Q(members__last_name="")), then=full_name),
            default=Value(None),
            output_field=CharField(),
        )

        
        qs = qs.annotate(
            university_names_distinct_count=Count(nonblank_university, distinct=True),
            university_names_agg=Coalesce(
                GroupConcat(nonblank_university, distinct=True, separator=", "),
                Value("—"),
            ),
            member_names_agg=Coalesce(
                GroupConcat(nonblank_full_name, distinct=True, separator=", "),
                Value("—"),
            ),
        )
        return qs

    
    def university_names(self, obj):
        return getattr(obj, "university_names_agg", "—")
    university_names.short_description = "Universities"
    university_names.admin_order_field = "university_names_agg"

    def are_university_names_unique(self, obj):
        return (getattr(obj, "university_names_distinct_count", 0) or 0) <= 1
    are_university_names_unique.boolean = True
    are_university_names_unique.short_description = "Univ unique?"
    are_university_names_unique.admin_order_field = "university_names_distinct_count"

    def member_names(self, obj):
        return getattr(obj, "member_names_agg", "—")
    member_names.short_description = "Members"
    member_names.admin_order_field = "member_names_agg"

    
    def approve_selected(self, request, queryset):
        count = 0
        for tr in queryset.select_related("competition", "submitter"):
            backoffice_approve_request(tr)
            count += 1
        self.message_user(request, f"Approved {count} request(s)")
    approve_selected.short_description = "Approve → send payment link"

    def reject_selected(self, request, queryset):
        count = 0
        for tr in queryset:
            backoffice_reject_request(tr, reason=getattr(tr, "_tmp_reject_reason", "Rejected"))
            count += 1
        self.message_user(request, f"Rejected {count} request(s)")
    reject_selected.short_description = "Reject with reason (set on object)"

    def mark_final_selected(self, request, queryset):
        count = 0
        for tr in queryset:
            mark_payment_final(tr)
            count += 1
        self.message_user(request, f"Marked FINAL for {count} request(s)")
    mark_final_selected.short_description = "Mark paid (FINAL)"