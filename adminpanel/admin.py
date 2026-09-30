"""
Staff roles (Django groups), shown as "Staff roles". Editing a role changes
what its members may do, so only superusers can; auditors may view them.
"""

from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin
from django.contrib.auth.models import Group
from django.db.models import Count
from django.utils.html import format_html
from django.utils.safestring import mark_safe

# Imported first so their registrations exist to be replaced below (this
# app's admin module is discovered before theirs).
import rest_framework_simplejwt.token_blacklist.admin  # noqa: F401
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from .models import StaffRole
from .roles import DEFAULT_ROLES


# Shown as StaffRole instead.
admin.site.unregister(Group)

# SimpleJWT's token tables: Sessions cover them, and revoking a session
# there also ends it. Hidden to avoid blacklisting without revoking.
for model in (OutstandingToken, BlacklistedToken):
    if admin.site.is_registered(model):
        admin.site.unregister(model)


@admin.register(StaffRole)
class StaffRoleAdmin(GroupAdmin):

    list_display = ["name", "about", "members", "permission_count"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            member_count=Count("user", distinct=True),
            permissions_total=Count("permissions", distinct=True),
        )

    @admin.display(description="Role", ordering="name")
    def about(self, group):
        role = DEFAULT_ROLES.get(group.name)

        if role:
            return format_html('<span class="jv-muted">{}</span>', role["description"])

        return mark_safe('<span class="jv-muted">Custom role</span>')

    @admin.display(description="Members", ordering="member_count")
    def members(self, group):
        return group.member_count

    @admin.display(description="Permissions", ordering="permissions_total")
    def permission_count(self, group):
        return group.permissions_total

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
