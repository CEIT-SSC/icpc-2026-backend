import csv
from datetime import datetime, time
from html import unescape
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from competitions.models import Competition
from presentations.models import Course, CourseSession, Registration


class AdminCsvExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = get_user_model().objects.create_superuser(
            email="admin@example.com",
            password="test-password",
        )

    def setUp(self):
        self.client.force_login(self.superuser)

    def make_course(self, name, slug, *, is_active=True):
        return Course.objects.create(
            name=name,
            slug=slug,
            offering_type=Course.OfferingType.ONLINE_PRESENTATION,
            capacity=12,
            price=85_000,
            is_active=is_active,
        )

    def export(self, model, params=None):
        return self.client.get(
            reverse(
                "admin:export_csv",
                args=(model._meta.app_label, model._meta.model_name),
            ),
            params or {},
        )

    def csv_rows(self, response):
        content = b"".join(response.streaming_content)
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        return list(csv.reader(StringIO(content.decode("utf-8-sig"))))

    def test_export_control_appears_and_preserves_changelist_parameters(self):
        changelist_url = reverse("admin:presentations_course_changelist")
        export_url = reverse(
            "admin:export_csv", args=("presentations", "course")
        )
        params = {"q": "دوره", "is_active__exact": "1", "o": "-1"}

        response = self.client.get(changelist_url, params)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Export CSV")
        page = unescape(response.content.decode())
        href_start = page.index(f'href="{export_url}') + len('href="')
        href = page[href_start : page.index('"', href_start)]
        self.assertEqual(
            parse_qs(urlsplit(href).query),
            {key: [value] for key, value in params.items()},
        )

    def test_csv_headers_unicode_choices_booleans_and_custom_columns(self):
        self.make_course("دوره الگوریتم پیشرفته", "advanced-algorithms")

        response = self.export(Course)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertRegex(
            response["Content-Disposition"],
            r'^attachment; filename="presentations-course-\d{8}-\d{6}\.csv"$',
        )
        rows = self.csv_rows(response)
        self.assertEqual(
            rows[0],
            [
                "Name",
                "Bundle type",
                "Offering type",
                "Effective capacity",
                "Remaining capacity",
                "Price",
                "Is active",
            ],
        )
        self.assertEqual(
            rows[1],
            [
                "دوره الگوریتم پیشرفته",
                "",
                "Online presentation",
                "12",
                "12",
                "85000",
                "Yes",
            ],
        )

    def test_search_and_filter_parameters_limit_exported_records(self):
        expected = self.make_course("کارگاه ویژه فعال", "special-active")
        self.make_course("دوره معمولی", "ordinary-active")
        self.make_course(
            "کارگاه ویژه غیرفعال", "special-inactive", is_active=False
        )

        rows = self.csv_rows(
            self.export(
                Course,
                {"q": "ویژه", "is_active__exact": "1"},
            )
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], expected.name)

    def test_export_is_not_limited_to_the_requested_pagination_page(self):
        courses = [
            self.make_course(f"Paged course {index}", f"paged-{index}")
            for index in range(3)
        ]
        course_admin = admin.site._registry[Course]
        params = {"q": "Paged course", "p": "2"}

        with patch.object(course_admin, "list_per_page", 1):
            changelist = self.client.get(
                reverse("admin:presentations_course_changelist"), params
            )
            response = self.export(Course, params)

        self.assertEqual(len(changelist.context["cl"].result_list), 1)
        rows = self.csv_rows(response)
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            {row[0] for row in rows[1:]},
            {course.name for course in courses},
        )

    def test_requested_changelist_ordering_is_preserved(self):
        for name, slug in (
            ("Order Alpha", "order-alpha"),
            ("Order Charlie", "order-charlie"),
            ("Order Bravo", "order-bravo"),
        ):
            self.make_course(name, slug)

        rows = self.csv_rows(self.export(Course, {"q": "Order", "o": "-1"}))

        self.assertEqual(
            [row[0] for row in rows[1:]],
            ["Order Charlie", "Order Bravo", "Order Alpha"],
        )

    def test_date_hierarchy_constraints_are_preserved(self):
        course = self.make_course("Dated course", "dated-course")
        old_user = get_user_model().objects.create_user(email="old@example.com")
        new_user = get_user_model().objects.create_user(email="new@example.com")
        old_registration = Registration.objects.create(
            course=course,
            user=old_user,
            submitted_at=timezone.make_aware(datetime(2025, 5, 10, 9, 0)),
        )
        Registration.objects.create(
            course=course,
            user=new_user,
            submitted_at=timezone.make_aware(datetime(2026, 5, 10, 9, 0)),
        )

        rows = self.csv_rows(
            self.export(Registration, {"submitted_at__year": "2025"})
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], str(old_registration.pk))
        self.assertEqual(rows[1][3], old_user.email)
        self.assertTrue(rows[1][8].startswith("2025-05-10 09:00:00"))

    def test_related_fields_dates_and_times_are_serialized(self):
        course = self.make_course("Related course", "related-course")
        CourseSession.objects.create(
            course=course,
            date=datetime(2026, 9, 1).date(),
            start_time=time(9, 30),
            end_time=time(11, 15),
        )

        rows = self.csv_rows(self.export(CourseSession))

        self.assertEqual(rows[0], ["Course", "Start time", "End time"])
        self.assertEqual(rows[1], ["Related course", "09:30:00", "11:15:00"])

    def test_decimal_values_keep_their_declared_precision(self):
        Competition.objects.create(
            name="Decimal competition",
            slug="decimal-competition",
            signup_fee_aut="1234.50",
            signup_fee_base="0.00",
        )

        rows = self.csv_rows(self.export(Competition))

        self.assertEqual(rows[1][4:6], ["1234.50", "0.00"])
        self.assertEqual(rows[1][6:], ["Yes", "Yes"])

    def test_unauthenticated_and_unpermitted_users_cannot_export(self):
        export_url = reverse(
            "admin:export_csv", args=("presentations", "course")
        )
        self.client.logout()
        anonymous_response = self.client.get(export_url)

        denied_user = get_user_model().objects.create_user(
            email="denied@example.com",
            is_staff=True,
        )
        self.client.force_login(denied_user)
        denied_response = self.client.get(export_url)

        self.assertEqual(anonymous_response.status_code, 302)
        self.assertIn(reverse("admin:login"), anonymous_response.url)
        self.assertEqual(denied_response.status_code, 403)

    def test_view_permission_allows_export(self):
        course = self.make_course("Viewable course", "viewable-course")
        view_user = get_user_model().objects.create_user(
            email="viewer@example.com",
            is_staff=True,
        )
        view_user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="presentations",
                codename="view_course",
            )
        )
        self.client.force_login(view_user)

        rows = self.csv_rows(self.export(Course))

        self.assertEqual(rows[1][0], course.name)

    def test_model_admin_queryset_restrictions_apply_to_export(self):
        visible = self.make_course("Visible course", "visible-course")
        self.make_course("Hidden course", "hidden-course", is_active=False)
        course_admin = admin.site._registry[Course]
        original_get_queryset = course_admin.get_queryset

        def active_courses_only(request):
            return original_get_queryset(request).filter(is_active=True)

        with patch.object(
            course_admin,
            "get_queryset",
            side_effect=active_courses_only,
        ):
            rows = self.csv_rows(self.export(Course))

        self.assertEqual([row[0] for row in rows[1:]], [visible.name])
