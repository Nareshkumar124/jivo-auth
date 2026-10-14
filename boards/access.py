"""
Who sees the JI dashboards: anyone holding ``boards.view_dashboards``
(given in the ticket system's People and roles, or in /admin/), and every
superuser. Checked by the dashboard views and the ticket system's sidebar.
"""

from django.contrib.auth.models import Permission

PERMISSION = "boards.view_dashboards"
CODENAME = "view_dashboards"


def can_view_dashboards(user) -> bool:
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and user.has_perm(PERMISSION)
    )


def permission() -> Permission:
    return Permission.objects.get(content_type__app_label="boards", codename=CODENAME)


def has_direct_access(user) -> bool:
    """Given to this user (directly or through a staff role), not only as a superuser."""

    return user.user_permissions.filter(content_type__app_label="boards", codename=CODENAME).exists() or user.groups.filter(
        permissions__content_type__app_label="boards", permissions__codename=CODENAME
    ).exists()


def grant(user) -> None:
    user.user_permissions.add(permission())
    _forget(user)


def revoke(user) -> bool:
    """Remove the direct grant. Returns whether access remains (a staff role or superuser)."""

    user.user_permissions.remove(permission())
    _forget(user)
    return can_view_dashboards(user)


def _forget(user) -> None:
    for name in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
        if hasattr(user, name):
            delattr(user, name)
