from rest_framework import serializers
from django.conf import settings
from django.contrib.auth import get_user_model
from .models import Course, Presenter, ScheduleRule, Registration, RegistrationItem, CourseSession

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
    schedule = ScheduleRuleSerializer(many=True, read_only=True)
    offering_type_display = serializers.CharField(
        source="get_offering_type_display", read_only=True
    )
    currency = serializers.SerializerMethodField()

    class Meta:
        model = Course
        fields = (
            "id",
            "name",
            "offering_type",
            "offering_type_display",
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


class CourseSerializer(serializers.ModelSerializer):
    presenters = PresenterSerializer(many=True, read_only=True)
    schedule = ScheduleRuleSerializer(many=True, read_only=True)
    offering_type_display = serializers.CharField(
        source="get_offering_type_display", read_only=True
    )
    currency = serializers.SerializerMethodField()
    requires_approval = serializers.SerializerMethodField()

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
            "schedule",
        )

    def get_currency(self, obj: Course) -> str:
        return settings.PAYMENT_CURRENCY

    def get_requires_approval(self, obj: Course) -> bool:
        # Kept in the response for compatibility; manual approval is retired.
        return False


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
            raise serializers.ValidationError(
                {
                    "child_ids": (
                        "Packages are not available; register for one offering "
                        "using course_id only."
                    )
                }
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
