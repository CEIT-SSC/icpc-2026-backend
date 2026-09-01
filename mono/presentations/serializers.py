from rest_framework import serializers, status
from django.conf import settings
from django.contrib.auth import get_user_model
from .models import (
    Course,
    CourseSession,
    Presenter,
    Registration,
    RegistrationItem,
    ScheduleRule,
)
from acm import error_codes as EC
from acm.exceptions import CustomAPIException

User = get_user_model()


class PresenterSerializer(serializers.ModelSerializer):
    class Meta:
        model = Presenter
        fields = ("id", "full_name", "bio", "email", "website")


class ScheduleRuleSerializer(serializers.ModelSerializer):
    weekday_display = serializers.CharField(source="get_weekday_display", read_only=True)

    class Meta:
        model = ScheduleRule
        fields = ("weekday", "weekday_display", "start_time", "end_time")


class ChildCourseSerializer(serializers.ModelSerializer):
    presenters = PresenterSerializer(many=True, read_only=True)
    schedule = ScheduleRuleSerializer(many=True, read_only=True)
    offering_type_display = serializers.CharField(
        source="get_offering_type_display", read_only=True
    )
    currency = serializers.SerializerMethodField()
    capacity = serializers.SerializerMethodField()
    remained_capacity = serializers.SerializerMethodField()
    is_unlimited = serializers.BooleanField(read_only=True)

    class Meta:
        model = Course
        fields = (
            "id",
            "name",
            "subtitle",
            "description",
            "presenters",
            "offering_type",
            "offering_type_display",
            "online",
            "onsite",
            "capacity",
            "remained_capacity",
            "is_unlimited",
            "price",
            "currency",
            "slug",
            "is_active",
            "schedule",
        )

    def get_currency(self, obj: Course) -> str:
        return settings.PAYMENT_CURRENCY

    def get_capacity(self, obj: Course) -> int | None:
        return obj.effective_capacity()

    def get_remained_capacity(self, obj: Course) -> int | None:
        return obj.remained_capacity()


class CourseSerializer(serializers.ModelSerializer):
    presenters = PresenterSerializer(many=True, read_only=True)
    schedule = ScheduleRuleSerializer(many=True, read_only=True)
    offering_type_display = serializers.CharField(
        source="get_offering_type_display", read_only=True
    )
    bundle_type_display = serializers.CharField(
        source="get_bundle_type_display", read_only=True, allow_null=True
    )
    currency = serializers.SerializerMethodField()
    requires_approval = serializers.SerializerMethodField()
    capacity = serializers.SerializerMethodField()
    remained_capacity = serializers.SerializerMethodField()
    is_unlimited = serializers.BooleanField(read_only=True)
    category = serializers.CharField(read_only=True, allow_null=True)
    delivery_mode = serializers.CharField(read_only=True, allow_null=True)
    member_count = serializers.SerializerMethodField()
    members = ChildCourseSerializer(source="children", many=True, read_only=True)

    class Meta:
        model = Course
        fields = (
            "id",
            "name",
            "subtitle",
            "description",
            "presenters",
            "start_date",
            "online",
            "onsite",
            "classes_count",
            "bundle_type",
            "bundle_type_display",
            "category",
            "delivery_mode",
            "offering_type",
            "offering_type_display",
            "capacity",
            "remained_capacity",
            "is_unlimited",
            "price",
            "currency",
            "requires_approval",
            "slug",
            "is_active",
            "member_count",
            "members",
            "schedule",
        )

    def get_currency(self, obj: Course) -> str:
        return settings.PAYMENT_CURRENCY

    def get_requires_approval(self, obj: Course) -> bool:
        # Kept in the response for compatibility; manual approval is retired.
        return False

    def get_capacity(self, obj: Course) -> int | None:
        return obj.effective_capacity()

    def get_remained_capacity(self, obj: Course) -> int | None:
        return obj.remained_capacity()

    def get_member_count(self, obj: Course) -> int:
        if not obj.is_bundle:
            return 0
        return len(obj.children.all())


class RegistrationItemSerializer(serializers.ModelSerializer):
    child = ChildCourseSerializer(source="child_course", read_only=True)

    class Meta:
        model = RegistrationItem
        fields = ("id", "child", "price", "created_at")


class RegistrationCreateSerializer(serializers.Serializer):
    course_id = serializers.IntegerField()
    extra_answers = serializers.DictField(
        child=serializers.JSONField(), required=False
    )

    def to_internal_value(self, data):
        if "child_ids" in data:
            raise CustomAPIException(
                code=EC.REG_PACKAGE_UNAVAILABLE,
                message=(
                    "Do not send child_ids; the server selects every bundle "
                    "member from course_id."
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return super().to_internal_value(data)


class RegistrationSerializer(serializers.ModelSerializer):
    course = CourseSerializer(read_only=True)
    items = RegistrationItemSerializer(many=True, read_only=True)
    total_amount = serializers.SerializerMethodField()
    waitlist_position = serializers.IntegerField(read_only=True)

    class Meta:
        model = Registration
        fields = (
            "id",
            "course",
            "user",
            "price",
            "currency",
            "status",
            "waitlist_position",
            "resume_url",
            "payment_link",
            "rejection_reason",
            "submitted_at",
            "decided_at",
            "items",
            "total_amount",
        )
        read_only_fields = (
            "user",
            "price",
            "currency",
            "status",
            "waitlist_position",
            "payment_link",
            "rejection_reason",
            "submitted_at",
            "decided_at",
            "items",
            "total_amount",
        )

    def get_total_amount(self, obj: Registration) -> int:
        base = obj.price if obj.price is not None else (obj.course.price or 0)
        extra = sum((i.price or 0) for i in obj.items.all())
        return base + extra

    currency = serializers.SerializerMethodField()

    def get_currency(self, obj: Registration) -> str:
        return settings.PAYMENT_CURRENCY


class RegistrationPaymentSerializer(serializers.Serializer):
    registration_id = serializers.IntegerField()
    payment_id = serializers.IntegerField()
    authority = serializers.CharField()
    payment_link = serializers.URLField()
    amount = serializers.IntegerField()
    currency = serializers.CharField()
    status = serializers.CharField()


class SkyroomLinkGeneratorSerializer(serializers.Serializer):
    course_id = serializers.CharField(max_length=100)

    class Meta:
        fields = ("course_id",)

class SkyroomLinkGeneratorResponseSerializer(serializers.Serializer):
    link = serializers.CharField(max_length=100)

    class Meta:
        fields = ("link",)

class CourseSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CourseSession
        fields = "__all__"


class CourseSessionResponseSerializer(serializers.Serializer):
    sessions = CourseSessionSerializer(many=True, read_only=True)

class RegisterCourseSerializer(serializers.Serializer):
    discount_code = serializers.CharField(required=False, allow_blank=True)