from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from adminpanel.admin_tools import badge, confirm_action, relative_time, titled

from .models import Application


class ApiKeyFilter(admin.SimpleListFilter):
    title = "API key"
    parameter_name = "api_key"

    def lookups(self, request, model_admin):
        return [("yes", "Has a key"), ("no", "No key yet")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(api_key_hash__isnull=False)

        if self.value() == "no":
            return queryset.filter(api_key_hash__isnull=True)

        return queryset


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):

    list_display = [
        "application",
        "state",
        "user_count",
        "api_key",
        "created",
    ]

    list_display_links = ["application"]

    list_filter = [
        ("is_active", titled("active")),
        ApiKeyFilter,
    ]

    search_fields = [
        "slug",
        "name",
    ]

    autocomplete_fields = [
        "users",
    ]

    readonly_fields = [
        "api_key_status",
        "created_at",
    ]

    fieldsets = [
        (None, {"fields": ["name", "slug", "is_active"]}),
        (
            "Users with access",
            {
                "fields": ["users"],
                "description": (
                    "Changes reach users' tokens on their next refresh "
                    "(within 15 minutes)."
                ),
            },
        ),
        ("API key", {"fields": ["api_key_status", "created_at"]}),
    ]

    actions = [
        "activate",
        "deactivate",
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            active_users=Count("users", filter=Q(users__is_active=True), distinct=True),
        )

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))

        # Tokens and every client's JIVO_AUTH["APP"] carry the slug.
        if obj is not None:
            fields.append("slug")

        return fields

    # ---- Columns ----------------------------------------------------------

    @admin.display(description="Application", ordering="name")
    def application(self, app):
        return format_html('{}<br><code class="jv-muted">{}</code>', app.name, app.slug)

    @admin.display(description="Status", ordering="is_active")
    def state(self, app):
        return badge("Active", "good") if app.is_active else badge("Inactive", "neutral")

    @admin.display(description="Users", ordering="active_users")
    def user_count(self, app):
        return app.active_users

    @admin.display(description="API key")
    def api_key(self, app):
        if not app.api_key_hash:
            return badge("No key", "warning")

        return format_html(
            '<code>{}…</code> <span class="jv-muted">{}</span>',
            app.api_key_prefix,
            relative_time(app.api_key_created_at),
        )

    @admin.display(description="Created", ordering="created_at")
    def created(self, app):
        return relative_time(app.created_at)

    @admin.display(description="Current key")
    def api_key_status(self, app):
        if not app.api_key_hash:
            return format_html(
                "{} <span class='jv-muted'>Use “Create API key” above to make one.</span>",
                badge("No key", "warning"),
            )

        return format_html(
            "<code>{}…</code> created {}",
            app.api_key_prefix,
            relative_time(app.api_key_created_at),
        )

    # ---- Bulk actions -----------------------------------------------------

    @admin.action(description="Activate selected applications", permissions=["change"])
    def activate(self, request, queryset):
        count = queryset.filter(is_active=False).update(is_active=True)
        self.message_user(request, f"Activated {count} application(s).", messages.SUCCESS)

    @admin.action(description="Deactivate selected applications", permissions=["change"])
    def deactivate(self, request, queryset):
        def perform(selected, form):
            count = selected.filter(is_active=True).update(is_active=False)
            return f"Deactivated {count} application(s)."

        return confirm_action(
            self,
            request,
            queryset,
            title="Deactivate applications",
            message=(
                "Inactive applications disappear from users' tokens on their "
                "next refresh, and their API keys stop working now, so the "
                "applications refuse everyone within 15 minutes."
            ),
            confirm_label="Deactivate",
            danger=True,
            perform=perform,
        )

    # ---- API key rotation -------------------------------------------------

    def get_urls(self):
        return [
            path(
                "<id>/rotate-key/",
                self.admin_site.admin_view(self.rotate_key),
                name="applications_application_rotate_key",
            ),
            *super().get_urls(),
        ]

    def change_view(self, request, object_id, form_url="", extra_context=None):
        application = self.get_object(request, object_id)
        tools = []

        if application is not None and self.has_change_permission(request, application):
            has_key = bool(application.api_key_hash)

            tools.append(
                {
                    "label": "Rotate API key" if has_key else "Create API key",
                    "icon": "key",
                    "url": reverse("admin:applications_application_rotate_key", args=[application.pk]),
                    "danger": has_key,
                    "confirm": (
                        f"Replace the API key of {application.name}? The current "
                        "key stops working immediately, so update the "
                        "application at the same time."
                        if has_key
                        else ""
                    ),
                }
            )

        return super().change_view(
            request,
            object_id,
            form_url,
            {**(extra_context or {}), "object_tools": tools},
        )

    def rotate_key(self, request, id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        application = get_object_or_404(Application, pk=id)

        if not self.has_change_permission(request, application):
            raise PermissionDenied

        # Shown on this response only: not stored, not put in a message.
        raw_key = application.set_new_api_key()

        response = TemplateResponse(
            request,
            "adminpanel/api_key.html",
            {
                **self.admin_site.each_context(request),
                "title": "New API key",
                "opts": self.model._meta,
                "application": application,
                "api_key": raw_key,
            },
        )

        response["Cache-Control"] = "no-store"

        return response
