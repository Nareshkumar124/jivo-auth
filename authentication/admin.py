from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db.models import Count
from django.utils import timezone
from django.utils.html import format_html

from adminpanel.admin_tools import (
    badge,
    confirm_action,
    describe_user_agent,
    recent_filter,
    relative_time,
)
from audit.models import AuditEvent
from audit.services import record

from .models import UserSession
from .services import revoke_sessions


def expiry_cutoff():
    """Sessions not refreshed since then have an expired refresh token."""

    return timezone.now() - settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]


class StatusFilter(admin.SimpleListFilter):
    title = "status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return [
            ("active", "Active"),
            ("expired", "Expired"),
            ("revoked", "Revoked"),
        ]

    def queryset(self, request, queryset):
        if self.value() == "active":
            return queryset.filter(revoked_at__isnull=True, last_used_at__gte=expiry_cutoff())

        if self.value() == "expired":
            return queryset.filter(revoked_at__isnull=True, last_used_at__lt=expiry_cutoff())

        if self.value() == "revoked":
            return queryset.filter(revoked_at__isnull=False)

        return queryset


@admin.register(UserSession)
class UserSessionAdmin(admin.ModelAdmin):

    list_display = [
        "account",
        "device",
        "ip_address",
        "state",
        "started",
        "last_used",
    ]

    list_display_links = ["device"]

    list_filter = [
        StatusFilter,
        recent_filter("last_used_at", "last used", "used"),
        recent_filter("created_at", "started", "started"),
    ]

    search_fields = [
        "user__email",
        "device_name",
        "ip_address",
        "user_agent",
    ]

    list_select_related = [
        "user",
    ]

    list_per_page = 50

    readonly_fields = [
        "user",
        "device_name",
        "ip_address",
        "user_agent",
        "created_at",
        "last_used_at",
        "revoked_at",
    ]

    fields = readonly_fields

    actions = [
        "revoke",
    ]

    # Sessions are created by logging in and ended by revoking them.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    # Sessions can't be edited, so revoking needs its own check.
    def has_revoke_permission(self, request):
        return request.user.has_perm("authentication.change_usersession")

    # ---- Columns ----------------------------------------------------------

    @admin.display(description="User", ordering="user__email")
    def account(self, session):
        return session.user.email

    @admin.display(description="Device", ordering="device_name")
    def device(self, session):
        client = describe_user_agent(session.user_agent)

        if session.device_name:
            return format_html(
                '{}<br><span class="jv-muted">{}</span>',
                session.device_name,
                client,
            )

        return client or "Unknown device"

    @admin.display(description="Status")
    def state(self, session):
        if session.revoked_at:
            return badge("Revoked", "neutral")

        if session.last_used_at < expiry_cutoff():
            return badge("Expired", "neutral")

        return badge("Active", "good")

    @admin.display(description="Started", ordering="created_at")
    def started(self, session):
        return relative_time(session.created_at)

    @admin.display(description="Last used", ordering="last_used_at")
    def last_used(self, session):
        return relative_time(session.last_used_at)

    # ---- Actions ------------------------------------------------------------

    @admin.action(description="Revoke selected sessions", permissions=["revoke"])
    def revoke(self, request, queryset):
        def perform(selected, form):
            per_user = (
                selected.filter(revoked_at__isnull=True)
                .values("user")
                .annotate(count=Count("pk"))
            )
            counts = {row["user"]: row["count"] for row in per_user}

            revoked = revoke_sessions(selected)

            for user in get_user_model().objects.filter(pk__in=counts):
                record(
                    AuditEvent.Type.SESSIONS_REVOKED,
                    user=user,
                    count=counts[user.pk],
                )

            return f"Revoked {revoked} session(s)."

        return confirm_action(
            self,
            request,
            queryset,
            title="Revoke sessions",
            message=(
                "These devices are signed out: their refresh tokens stop "
                "working now, and access tokens already issued expire within "
                "15 minutes."
            ),
            confirm_label="Revoke sessions",
            danger=True,
            perform=perform,
        )
