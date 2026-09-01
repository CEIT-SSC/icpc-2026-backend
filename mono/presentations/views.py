# presentations/views.py

from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import generics, permissions, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Course, CourseSession, Registration, attach_capacity_snapshots
from .serializers import (
    CourseSerializer,
    RegistrationCreateSerializer,
    RegistrationPaymentSerializer,
    RegistrationSerializer, SkyroomLinkGeneratorSerializer, SkyroomLinkGeneratorResponseSerializer,
    CourseSessionSerializer, CourseSessionResponseSerializer,
)
from .services import (
    create_skyroom_link,
    get_course_sessions,
    initiate_registration_payment,
    submit_registration,
    validate_and_apply_discount,
    InvalidDiscountCode,
)

User = get_user_model()


def purchasable_offerings():
    return Course.objects.filter(
        is_active=True,
        bundle_type__in=Course.BundleType.values,
        offering_type__isnull=True,
        capacity__isnull=True,
        price__isnull=False,
    ).prefetch_related(
        "presenters",
        "schedule",
        "children__presenters",
        "children__schedule",
        "children__parents",
    )


class CourseListView(generics.ListAPIView):
    serializer_class = CourseSerializer
    permission_classes = []

    def get_queryset(self):
        return purchasable_offerings().order_by("id")

    def list(self, request, *args, **kwargs):
        bundles = [course for course in self.get_queryset() if course.is_valid_bundle()]
        attach_capacity_snapshots(bundles)
        return Response(self.get_serializer(bundles, many=True).data)

    @extend_schema(
        responses={200: CourseSerializer(many=True)},
        description="List the three active, valid all-access bundles.",
    )
    def get(self, *args, **kwargs):
        return super().get(*args, **kwargs)


class CourseDetailView(generics.RetrieveAPIView):
    queryset = purchasable_offerings()
    serializer_class = CourseSerializer
    lookup_field = "slug"
    permission_classes = []

    def get_object(self):
        course = super().get_object()
        if not course.is_valid_bundle():
            from django.http import Http404

            raise Http404
        attach_capacity_snapshots([course])
        return course

    @extend_schema(
        responses={200: CourseSerializer},
        description="Get a purchasable bundle by slug, including members and schedule."
    )
    def get(self, *args, **kwargs):
        return super().get(*args, **kwargs)


class MyRegistrationsView(generics.ListAPIView):
    serializer_class = RegistrationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Registration.objects.none()
        return (
            Registration.objects.filter(user=self.request.user)
            .select_related("course")
            .prefetch_related(
                "course__presenters",
                "course__schedule",
                "course__children__presenters",
                "course__children__schedule",
                "course__children__parents",
                "items__child_course__presenters",
                "items__child_course__schedule",
            )
        )

    def list(self, request, *args, **kwargs):
        registrations = list(self.get_queryset())
        waitlisted_ids = {
            registration.id
            for registration in registrations
            if registration.status == Registration.Status.RESERVED
        }
        positions = {}
        next_position = {}
        if waitlisted_ids:
            course_ids = {
                registration.course_id
                for registration in registrations
                if registration.id in waitlisted_ids
            }
            for registration_id, course_id in Registration.objects.filter(
                course_id__in=course_ids,
                status=Registration.Status.RESERVED,
            ).order_by("course_id", "submitted_at", "id").values_list(
                "id", "course_id"
            ):
                next_position[course_id] = next_position.get(course_id, 0) + 1
                if registration_id in waitlisted_ids:
                    positions[registration_id] = next_position[course_id]
            for registration in registrations:
                if registration.id in positions:
                    registration._waitlist_position_snapshot = positions[registration.id]
        capacity_courses = []
        for registration in registrations:
            capacity_courses.append(registration.course)
            capacity_courses.extend(
                item.child_course for item in registration.items.all()
            )
        attach_capacity_snapshots(capacity_courses)
        return Response(self.get_serializer(registrations, many=True).data)

    @extend_schema(
        responses={200: RegistrationSerializer(many=True)},
        description="List the authenticated user's course registrations."
    )
    def get(self, *args, **kwargs):
        return super().get(*args, **kwargs)


class RegistrationCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=RegistrationCreateSerializer,
        responses={
            200: RegistrationSerializer,
            400: OpenApiResponse(description="Validation error"),
            404: OpenApiResponse(description="Course not found"),
        },
        description="Submit a registration for a course. Also persists extra answers to UserExtraData."
    )
    def post(self, request):
        serializer = RegistrationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        course = get_object_or_404(
            Course,
            id=data["course_id"],
            is_active=True,
        )
        reg = submit_registration(
            course=course,
            user=request.user,
            extra_updates=data.get("extra_answers"),
        )
        reg = (
            Registration.objects.select_related("course")
            .prefetch_related(
                "course__presenters",
                "course__schedule",
                "course__children__presenters",
                "course__children__schedule",
                "course__children__parents",
                "items__child_course__presenters",
                "items__child_course__schedule",
            )
            .get(pk=reg.pk)
        )
        attach_capacity_snapshots(
            [reg.course, *(item.child_course for item in reg.items.all())]
        )
        return Response(RegistrationSerializer(reg).data, status=status.HTTP_200_OK)


class RegistrationPaymentView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=None,
        responses={
            201: RegistrationPaymentSerializer,
            404: OpenApiResponse(description="Registration not found for this user"),
            409: OpenApiResponse(
                description="Registration is not payment-eligible or capacity is unavailable"
            ),
        },
        description=(
            "Create a payment gateway link on demand for the authenticated user's "
            "approved registration. No request body is required."
        ),
    )
    def post(self, request, registration_id: int):
        result = initiate_registration_payment(
            registration_id=registration_id,
            user=request.user,
        )
        payload = {
            "registration_id": registration_id,
            "payment_id": result.payment.id,
            "authority": result.authority,
            "payment_link": result.url,
            "amount": result.payment.amount,
            "currency": result.payment.currency,
            "status": result.payment.status,
        }
        return Response(
            RegistrationPaymentSerializer(payload).data,
            status=status.HTTP_201_CREATED,
        )


class SkyroomLinkView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=SkyroomLinkGeneratorSerializer,
        responses={
            200: SkyroomLinkGeneratorResponseSerializer,
            400: OpenApiResponse(description="Validation error"),
            404: OpenApiResponse(description="Course not found"),
        },
        description="Submit a registration for a course. Also persists extra answers to UserExtraData."
    )
    def get(self, request):
        course = None
        course_slug = request.query_params.get("course")
        course_id = request.query_params.get("course_id")

        if course_slug:
            course = Course.objects.filter(slug=course_slug, is_active=True).first()
        elif course_id:
            course = Course.objects.filter(id=course_id, is_active=True).first()

        if course is None:
            return Response({"detail": "Course not found."}, status=status.HTTP_400_BAD_REQUEST)

        link = create_skyroom_link(request.user, course)
        if not link:
            return Response(
                {
                    "detail": "You are not registered for this presentation or it's not within the scheduled time window."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({"url": link}, status=status.HTTP_200_OK)


class CourseSessionsView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    queryset = CourseSession.objects.none()
    serializer_class = CourseSessionSerializer

    @extend_schema(
        responses={200: CourseSessionResponseSerializer(many=True)},
        description="List the authenticated user's course sessions."
    )
    def get(self, request, slug):
        course = get_object_or_404(Course, slug=slug, is_active=True)
        current_sessions = get_course_sessions(request.user, course)
        if current_sessions is None:
            return Response(status=status.HTTP_400_BAD_REQUEST, data={"detail": "User's not registered for this course"})
        return Response(current_sessions, status=status.HTTP_200_OK)

class ValidateDiscountView(APIView):
    def post(self, request):
        course_id = request.data.get("course_id")
        code = request.data.get("code")
        course = get_object_or_404(Course, id=course_id)
        try:
            final_price, discount = validate_and_apply_discount(course, code)
        except InvalidDiscountCode as e:
            return Response({"detail": str(e)}, status=400)
        return Response({
            "valid": True,
            "original_price": course.price,
            "final_price": final_price,
        })