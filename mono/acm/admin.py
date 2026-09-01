"""Project-wide Django Admin customizations."""

import csv
import enum
from datetime import date, datetime, time
from decimal import Decimal
from html import unescape

from django.apps import apps
from django.contrib.admin.sites import AdminSite, NotRegistered
from django.contrib.admin.utils import label_for_field, lookup_field
from django.core.exceptions import PermissionDenied
from django.http import Http404, StreamingHttpResponse
from django.urls import path
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.html import strip_tags
from django.utils.http import content_disposition_header
from django.utils.text import capfirst
from django.utils.translation import gettext


CSV_ITERATOR_CHUNK_SIZE = 2_000


class _CsvBuffer:
    """File-like object that makes ``csv.writer`` return each rendered row."""

    def write(self, value):
        return value


class ExportAdminSite(AdminSite):
    """Admin site that exports the current state of any model changelist."""

    def get_urls(self):
        custom_urls = [
            path(
                "export-csv/<str:app_label>/<str:model_name>/",
                self.admin_view(self.export_csv),
                name="export_csv",
            ),
        ]
        return custom_urls + super().get_urls()

    def export_csv(self, request, app_label, model_name):
        model_admin = self._get_model_admin(app_label, model_name)
        if not model_admin.has_view_or_change_permission(request):
            raise PermissionDenied

        changelist = model_admin.get_changelist_instance(request)
        columns = [
            column
            for column in changelist.list_display
            if column != "action_checkbox"
        ]
        headers = [
            capfirst(
                force_str(
                    label_for_field(
                        column,
                        model_admin.model,
                        model_admin=model_admin,
                    )
                )
            )
            for column in columns
        ]

        response = StreamingHttpResponse(
            self._csv_rows(model_admin, changelist.queryset, columns, headers),
            content_type="text/csv; charset=utf-8",
        )
        timestamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
        filename = f"{app_label}-{model_name}-{timestamp}.csv"
        response["Content-Disposition"] = content_disposition_header(
            as_attachment=True,
            filename=filename,
        )
        return response

    def _get_model_admin(self, app_label, model_name):
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError as exc:
            raise Http404("Unknown admin model.") from exc

        try:
            return self.get_model_admin(model)
        except NotRegistered as exc:
            raise Http404("This model is not registered in the admin.") from exc

    @staticmethod
    def _csv_rows(model_admin, queryset, columns, headers):
        writer = csv.writer(_CsvBuffer())

        # A UTF-8 BOM makes Persian and other Unicode text open reliably in Excel.
        yield "\ufeff"
        yield writer.writerow(headers)
        for obj in queryset.iterator(chunk_size=CSV_ITERATOR_CHUNK_SIZE):
            yield writer.writerow(
                [
                    _display_value(model_admin, obj, column)
                    for column in columns
                ]
            )


def _display_value(model_admin, obj, column):
    field, _attr, value = lookup_field(column, obj, model_admin)
    if field is not None and field.choices:
        display_method = getattr(obj, f"get_{field.name}_display", None)
        if display_method is not None:
            value = display_method()
    return _serialize_value(value)


def _serialize_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return gettext("Yes") if value else gettext("No")
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, enum.Enum):
        return force_str(getattr(value, "label", value.value))

    rendered = force_str(value)
    if hasattr(value, "__html__"):
        rendered = unescape(strip_tags(rendered))
    return rendered
