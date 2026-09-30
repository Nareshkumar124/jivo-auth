from django.contrib import admin

from .models import UserSession
from .services import revoke_sessions


@admin.register(UserSession)
class UserSessionAdmin(admin.ModelAdmin):

    list_display = [
        "user",
        "device_name",
        "ip_address",
        "created_at",
        "last_used_at",
        "revoked_at",
    ]

    list_filter = [
        ("revoked_at", admin.EmptyFieldListFilter),
    ]

    search_fields = [
        "user__email",
        "device_name",
        "ip_address",
    ]

    list_select_related = [
        "user",
    ]

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

    @admin.action(
        description="Revoke selected sessions",
        permissions=["revoke"],
    )
    def revoke(self, request, queryset):
        count = revoke_sessions(queryset)

        self.message_user(request, f"Revoked {count} session(s).")
