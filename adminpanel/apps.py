from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig


class AdminPanelConfig(AppConfig):
    """Templates, static files and default staff roles of the admin panel."""

    name = "adminpanel"
    verbose_name = "Staff"

    def ready(self):
        from django.db.models.signals import post_migrate

        from .roles import create_default_roles

        post_migrate.connect(
            create_default_roles,
            dispatch_uid="adminpanel.create_default_roles",
        )


class JivoAdminConfig(AdminConfig):
    """django.contrib.admin, with JivoAdminSite as the default site."""

    default_site = "adminpanel.sites.JivoAdminSite"
