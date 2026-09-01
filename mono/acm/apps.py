from django.contrib.admin.apps import AdminConfig


class ExportAdminConfig(AdminConfig):
    default_site = "acm.admin.ExportAdminSite"
