from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpResponseNotAllowed, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from adminpanel.admin_tools import (
    badge,
    confirm_action,
    describe_user_agent,
    recent_filter,
    relative_time,
    titled,
)
from applications.models import Application
from audit.models import AuditEvent
from audit.services import record
from authentication.models import UserSession
from authentication.services import revoke_all_sessions

from .models import User
from .services import send_email_verification, send_password_reset


STAFF_FIELDS = ("is_staff", "is_superuser", "groups", "user_permissions")


def can_manage_access(request):
    return request.user.has_perm("applications.change_application")


class UserCreationForm(AdminUserCreationForm):

    class Meta:
        model = User
        fields = ("email", "employee_code", "is_verified")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["is_verified"].initial = True
        self.fields["is_verified"].label = "Email verified"
        self.fields["is_verified"].help_text = (
            "Untick if you haven't confirmed the address; the user then "
            "has to verify it before they can log in."
        )


class ApplicationChoiceField(forms.ModelMultipleChoiceField):

    def label_from_instance(self, application):
        suffix = "" if application.is_active else " (inactive)"
        return f"{application.name} ({application.slug}){suffix}"


class UserEditForm(UserChangeForm):

    applications = ApplicationChoiceField(
        queryset=Application.objects.order_by("name"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=(
            "Applications this user may use. Changes reach their tokens on "
            "the next refresh (within 15 minutes)."
        ),
    )

    class Meta:
        model = User
        fields = "__all__"

    LABELS = {
        "is_active": (
            "Active",
            "Deactivated users can't sign in or refresh tokens anywhere.",
        ),
        "is_verified": (
            "Email verified",
            "Users can't sign in until their email address is verified.",
        ),
        "is_staff": ("Staff", "Can sign in to this admin panel."),
        "is_superuser": ("Superuser", "Has every permission, without assigning them."),
        "groups": ("Staff roles", "What this staff member may do in the admin panel."),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for name, (label, help_text) in self.LABELS.items():
            if name in self.fields:
                self.fields[name].label = label
                self.fields[name].help_text = help_text

        if self.instance.pk:
            self.fields["applications"].initial = self.instance.applications.all()


class ApplicationPickForm(forms.Form):
    """For the grant/remove access bulk actions."""

    application = forms.ModelChoiceField(
        queryset=Application.objects.filter(is_active=True).order_by("name"),
        empty_label=None,
    )


class UserSessionInline(admin.TabularInline):
    model = UserSession
    extra = 0
    max_num = 0
    can_delete = False
    ordering = ["-last_used_at"]
    fields = ["device", "ip_address", "created_at", "last_used_at", "status"]
    readonly_fields = fields
    verbose_name_plural = "Sessions"

    @admin.display(description="Device")
    def device(self, session):
        client = describe_user_agent(session.user_agent)

        if session.device_name and client:
            return f"{session.device_name} · {client}"

        return session.device_name or client or "-"

    @admin.display(description="Status")
    def status(self, session):
        return badge("Active", "good") if session.is_active else badge("Revoked", "neutral")

    def has_add_permission(self, request, obj=None):
        return False


class HasAccessFilter(admin.SimpleListFilter):
    title = "application access"
    parameter_name = "has_access"

    def lookups(self, request, model_admin):
        return [("yes", "Has access to an app"), ("no", "No application access")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(applications__isnull=False).distinct()

        if self.value() == "no":
            return queryset.filter(applications__isnull=True)

        return queryset


@admin.register(User)
class UserAdmin(DjangoUserAdmin):

    form = UserEditForm
    add_form = UserCreationForm

    list_display = [
        "account",
        "code",
        "status",
        "access",
        "active_sessions",
        "last_seen",
        "joined",
    ]

    list_display_links = ["account"]

    list_filter = [
        ("is_active", titled("active")),
        ("is_verified", titled("email verified")),
        HasAccessFilter,
        ("applications", titled("application", admin.RelatedFieldListFilter, empty=False)),
        recent_filter("created_at", "joined", "joined"),
        recent_filter("last_login", "last sign-in", "seen", never_label="Never signed in"),
        ("is_staff", titled("staff")),
        ("groups", titled("staff role", admin.RelatedFieldListFilter, empty=False)),
    ]

    search_fields = [
        "email",
        "first_name",
        "last_name",
        "employee_code",
    ]

    ordering = ["-created_at"]
    list_per_page = 25

    readonly_fields = [
        "last_login",
        "created_at",
        "updated_at",
        "recent_activity",
        "application_list",
    ]

    inlines = [
        UserSessionInline,
    ]

    add_fieldsets = [
        (
            None,
            {
                "classes": ["wide"],
                "fields": [
                    "email",
                    "employee_code",
                    "is_verified",
                    "usable_password",
                    "password1",
                    "password2",
                ],
            },
        ),
    ]

    actions = [
        "activate",
        "deactivate",
        "mark_verified",
        "send_verification",
        "send_password_reset",
        "log_out_everywhere",
        "grant_access",
        "revoke_access",
    ]

    # ---- Queryset and permissions ---------------------------------------

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .prefetch_related("applications")
            .annotate(
                live_sessions=Count(
                    "sessions",
                    filter=Q(sessions__revoked_at__isnull=True),
                    distinct=True,
                )
            )
        )

    def get_inlines(self, request, obj):
        # Only for an existing user who has signed in somewhere.
        if obj is None or not obj.sessions.exists():
            return []

        return self.inlines

    def has_change_permission(self, request, obj=None):
        # Only superusers may change a superuser: a reset password or a
        # removed flag would otherwise hand over the account.
        if obj is not None and obj.is_superuser and not request.user.is_superuser:
            return False

        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if (
            obj is not None
            and (obj.is_superuser or obj == request.user)
            and not request.user.is_superuser
        ):
            return False

        return super().has_delete_permission(request, obj)

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return self.add_fieldsets

        access = ["applications"] if can_manage_access(request) else ["application_list"]

        staff = ["is_staff", "is_superuser", "groups"]
        if request.user.is_superuser:
            staff.append("user_permissions")

        return [
            (None, {"fields": ["email", "password"]}),
            ("Profile", {"fields": ["first_name", "last_name", "employee_code"]}),
            ("Application access", {"fields": access}),
            ("Status", {"fields": ["is_active", "is_verified"]}),
            (
                "Staff and permissions",
                {
                    "fields": staff,
                    "classes": ["collapse"],
                    "description": (
                        "Staff can use this admin panel. Only superusers can "
                        "change these."
                    ),
                },
            ),
            (
                "Activity",
                {
                    "fields": (
                        ["recent_activity"]
                        if request.user.has_perm("audit.view_auditevent")
                        else []
                    )
                    + ["last_login", "created_at", "updated_at"],
                },
            ),
        ]

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))

        # Staff flags and roles are privilege management: superusers only.
        if not request.user.is_superuser:
            fields += [field for field in STAFF_FIELDS if field not in fields]

        return fields

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)

        # Only when the field was shown: otherwise it posts empty.
        if change and can_manage_access(request) and "applications" in form.cleaned_data:
            form.instance.applications.set(form.cleaned_data["applications"])

    # ---- Columns ----------------------------------------------------------

    @admin.display(description="User", ordering="email")
    def account(self, user):
        name = user.get_full_name()

        if name:
            return format_html(
                '{}<br><span class="jv-muted">{}</span>',
                user.email,
                name,
            )

        return user.email

    @admin.display(description="Employee code", ordering="employee_code")
    def code(self, user):
        if not user.employee_code:
            return mark_safe('<span class="jv-muted">-</span>')

        return format_html("<code>{}</code>", user.employee_code)

    @admin.display(description="Status")
    def status(self, user):
        badges = []

        if not user.is_active:
            badges.append(badge("Deactivated", "critical"))
        elif not user.is_verified:
            badges.append(badge("Unverified", "warning"))
        else:
            badges.append(badge("Active", "good"))

        if user.is_superuser:
            badges.append(badge("Superuser", "info"))
        elif user.is_staff:
            badges.append(badge("Staff", "info"))

        return format_html(
            '<span class="jv-chips">{}</span>',
            format_html_join("", "{}", ((item,) for item in badges)),
        )

    @admin.display(description="Applications")
    def access(self, user):
        apps = [app for app in user.applications.all() if app.is_active]

        if not apps:
            return mark_safe('<span class="jv-muted">None</span>')

        return format_html(
            '<span class="jv-chips">{}</span>',
            format_html_join(
                "",
                '<span class="jv-badge jv-badge--neutral">{}</span>',
                ((app.slug,) for app in apps),
            ),
        )

    @admin.display(description="Sessions", ordering="live_sessions")
    def active_sessions(self, user):
        return user.live_sessions

    @admin.display(description="Last sign-in", ordering="last_login")
    def last_seen(self, user):
        return relative_time(user.last_login)

    @admin.display(description="Joined", ordering="created_at")
    def joined(self, user):
        return relative_time(user.created_at)

    @admin.display(description="Applications")
    def application_list(self, user):
        return self.access(user)

    @admin.display(description="Recent activity")
    def recent_activity(self, user):
        if user.pk is None:
            return "-"

        events = list(user.audit_events.select_related("actor", "application")[:8])

        if not events:
            return mark_safe('<span class="jv-muted">No activity recorded yet.</span>')

        rows = format_html_join(
            "",
            '<li><span class="jv-badge jv-badge--{}">{}</span> <span>{}</span> <span class="jv-muted">{}</span></li>',
            (
                (
                    "neutral" if event.severity == "info" else event.severity,
                    event.get_type_display(),
                    event.summary,
                    relative_time(event.created_at),
                )
                for event in events
            ),
        )

        link = reverse("admin:audit_auditevent_changelist") + f"?user__id__exact={user.pk}"

        return format_html(
            '<ul class="jv-mini-log">{}</ul><a href="{}">Full history in the audit log</a>',
            rows,
            link,
        )

    # ---- Bulk actions -----------------------------------------------------

    def manageable(self, request, queryset):
        """Leave out accounts this admin may not change (superusers)."""

        if not request.user.is_superuser:
            queryset = queryset.filter(is_superuser=False)

        return queryset

    @admin.action(description="Activate selected users", permissions=["change"])
    def activate(self, request, queryset):
        users = list(self.manageable(request, queryset).filter(is_active=False))

        for user in users:
            user.is_active = True
            user.save(update_fields=["is_active", "updated_at"])

        self.message_user(request, f"Activated {len(users)} user(s).", messages.SUCCESS)

    @admin.action(description="Deactivate selected users", permissions=["change"])
    def deactivate(self, request, queryset):
        def perform(selected, form):
            users = list(
                self.manageable(request, selected)
                .filter(is_active=True)
                .exclude(pk=request.user.pk)
            )

            for user in users:
                user.is_active = False
                user.save(update_fields=["is_active", "updated_at"])
                revoke_all_sessions(user)

            return f"Deactivated {len(users)} user(s) and ended their sessions."

        return confirm_action(
            self,
            request,
            queryset,
            title="Deactivate users",
            message=(
                "Deactivated users can't sign in, and their sessions end now. "
                "Access tokens already issued stop working within 15 minutes. "
                "You can reactivate them later. Your own account is skipped."
            ),
            confirm_label="Deactivate",
            danger=True,
            perform=perform,
        )

    @admin.action(description="Mark email as verified", permissions=["change"])
    def mark_verified(self, request, queryset):
        users = list(self.manageable(request, queryset).filter(is_verified=False))

        for user in users:
            user.is_verified = True
            user.save(update_fields=["is_verified", "updated_at"])
            record(AuditEvent.Type.EMAIL_VERIFIED, user=user, source="admin")

        self.message_user(request, f"Marked {len(users)} user(s) as verified.", messages.SUCCESS)

    @admin.action(description="Send verification email", permissions=["change"])
    def send_verification(self, request, queryset):
        users = list(queryset.filter(is_verified=False, is_active=True))

        for user in users:
            send_email_verification(user)

        self.message_user(
            request,
            f"Sent a verification email to {len(users)} user(s).",
            messages.SUCCESS,
        )

    @admin.action(description="Send password reset email", permissions=["change"])
    def send_password_reset(self, request, queryset):
        def perform(selected, form):
            users = list(self.manageable(request, selected).filter(is_active=True))

            for user in users:
                send_password_reset(user)

            return f"Sent a password reset email to {len(users)} user(s)."

        return confirm_action(
            self,
            request,
            queryset,
            title="Send password reset emails",
            message=(
                "Each user gets a link to choose a new password. Their "
                "current password keeps working until they do."
            ),
            confirm_label="Send emails",
            perform=perform,
        )

    @admin.action(description="Log out selected users everywhere", permissions=["change"])
    def log_out_everywhere(self, request, queryset):
        def perform(selected, form):
            count = 0

            for user in self.manageable(request, selected):
                revoked = revoke_all_sessions(user)
                count += revoked

                if revoked:
                    record(AuditEvent.Type.SESSIONS_REVOKED, user=user, count=revoked, scope="all")

            return f"Revoked {count} session(s)."

        return confirm_action(
            self,
            request,
            queryset,
            title="Log out everywhere",
            message=(
                "Every session of these users ends now; they must sign in "
                "again on all devices. Access tokens already issued stay "
                "valid for up to 15 minutes."
            ),
            confirm_label="Log out everywhere",
            danger=True,
            perform=perform,
        )

    def has_access_permission(self, request):
        return can_manage_access(request)

    @admin.action(description="Grant access to an application…", permissions=["access"])
    def grant_access(self, request, queryset):
        def perform(selected, form):
            application = form.cleaned_data["application"]
            new = list(selected.exclude(applications=application))
            application.users.add(*new)

            return f"Granted {len(new)} user(s) access to {application.name}."

        return confirm_action(
            self,
            request,
            queryset,
            title="Grant application access",
            message=(
                "The selected users will be able to use the application from "
                "their next token refresh."
            ),
            confirm_label="Grant access",
            perform=perform,
            extra_form=ApplicationPickForm,
        )

    @admin.action(description="Remove access to an application…", permissions=["access"])
    def revoke_access(self, request, queryset):
        def perform(selected, form):
            application = form.cleaned_data["application"]
            current = list(selected.filter(applications=application))
            application.users.remove(*current)

            return f"Removed {len(current)} user(s) from {application.name}."

        return confirm_action(
            self,
            request,
            queryset,
            title="Remove application access",
            message=(
                "The selected users lose access on their next token refresh, "
                "within 15 minutes. To cut them off at once, also log them "
                "out everywhere."
            ),
            confirm_label="Remove access",
            danger=True,
            perform=perform,
            extra_form=ApplicationPickForm,
        )

    # ---- Per-user tools (buttons on the change form) ----------------------

    def get_urls(self):
        return [
            path(
                "<id>/tools/<str:tool_name>/",
                self.admin_site.admin_view(self.user_tool),
                name="users_user_tool",
            ),
            *super().get_urls(),
        ]

    def change_view(self, request, object_id, form_url="", extra_context=None):
        user = self.get_object(request, object_id)
        tools = []

        if user is not None and self.has_change_permission(request, user):
            def url(name):
                return reverse("admin:users_user_tool", args=[user.pk, name])

            if user.is_active:
                tools.append({"label": "Send password reset", "icon": "mail", "url": url("password-reset")})

            if user.is_active and not user.is_verified:
                tools.append({"label": "Resend verification", "icon": "mail", "url": url("verification")})

            tools.append(
                {
                    "label": "Log out everywhere",
                    "icon": "log-out",
                    "url": url("log-out"),
                    "danger": True,
                    "confirm": (
                        f"End every session of {user.email}? They'll have to "
                        "sign in again on all devices."
                    ),
                }
            )

        return super().change_view(
            request,
            object_id,
            form_url,
            {**(extra_context or {}), "object_tools": tools},
        )

    def user_tool(self, request, id, tool_name):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])

        user = get_object_or_404(User, pk=id)

        if not self.has_change_permission(request, user):
            raise PermissionDenied

        if tool_name == "password-reset" and user.is_active:
            send_password_reset(user)
            self.message_user(request, f"Sent a password reset email to {user.email}.", messages.SUCCESS)

        elif tool_name == "verification" and user.is_active and not user.is_verified:
            send_email_verification(user)
            self.message_user(request, f"Sent a verification email to {user.email}.", messages.SUCCESS)

        elif tool_name == "log-out":
            revoked = revoke_all_sessions(user)

            if revoked:
                record(AuditEvent.Type.SESSIONS_REVOKED, user=user, count=revoked, scope="all")

            self.message_user(request, f"Ended {revoked} session(s) of {user.email}.", messages.SUCCESS)

        else:
            self.message_user(request, "That action isn't available for this user.", messages.WARNING)

        return HttpResponseRedirect(reverse("admin:users_user_change", args=[user.pk]))
