from urllib.parse import urlencode

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from adminpanel.admin_tools import badge, confirm_action, relative_time, titled
from adminpanel.widgets import UserAccessField, UserAccessWidget, render_access_list

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
                    "People listed here can sign in to this application. "
                    "Changes reach their tokens on the next refresh (within "
                    f"{int(settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'].total_seconds() // 60)} minutes)."
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

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)

        # View-only staff get the same member list, without the controls.
        if obj is not None and not self.has_change_permission(request, obj):
            return [
                (name, {**options, "fields": ["people_with_access" if field == "users" else field for field in options["fields"]]})
                for name, options in fieldsets
            ]

        return fieldsets

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # Skip the related-object wrapper: its add/change popups expect a <select>.
        if db_field.name == "users":
            return self.formfield_for_manytomany(db_field, request, **kwargs)

        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name != "users":
            return super().formfield_for_manytomany(db_field, request, **kwargs)

        # Adding means searching accounts, which Django's autocomplete only
        # allows with permission to view users; the emails box follows suit.
        can_add = self.admin_site.get_model_admin(get_user_model()).has_view_permission(request)

        search_url = "{}?{}".format(
            reverse("admin:autocomplete", current_app=self.admin_site.name),
            urlencode(
                {
                    "app_label": self.opts.app_label,
                    "model_name": self.opts.model_name,
                    "field_name": db_field.name,
                }
            ),
        )

        return db_field.formfield(
            form_class=UserAccessField,
            widget=UserAccessWidget(search_url=search_url, can_add=can_add),
            allow_email_lookup=can_add,
            help_text="",
            **kwargs,
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

    @admin.display(description="People with access")
    def people_with_access(self, app):
        return render_access_list(app.users.all())

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
