"""
Navigation and page context of the ticket system. The sidebar is built
from the roles the signed-in person holds (support.access), so nobody sees
a link to a page that would refuse them.
"""

from django.conf import settings
from django.urls import reverse

from . import access
from .notifications import unread_count


def _item(request, label, icon, url_name, args=(), exact=False, badge=None):
    url = reverse(url_name, args=args)
    current = request.path == url if exact else request.path.startswith(url)

    return {"label": label, "icon": icon, "url": url, "current": current, "badge": badge}


def build_nav(request):
    user = request.user
    unread = unread_count(user)

    sections = [
        {
            "title": "Tickets",
            "items": [
                _item(request, "Home", "home", "support:home", exact=True),
                _item(request, "Raise ticket", "plus", "support:ticket-create"),
                _item(request, "My tickets", "ticket", "support:my-tickets", exact=True),
                _item(request, "Notifications", "bell", "support:notifications", badge=unread or None),
            ],
        }
    ]

    if access.is_team_member(user):
        sections.append(
            {
                "title": "Team",
                "items": [
                    _item(request, "Available tickets", "inbox", "support:team-tickets", args=["available"]),
                    _item(request, "My assigned tickets", "user-check", "support:team-tickets", args=["mine"]),
                    _item(request, "Team tickets", "layers", "support:team-tickets", args=["all"]),
                    _item(request, "Resolved tickets", "check-circle", "support:team-tickets", args=["resolved"]),
                    _item(request, "Team overview", "chart", "support:team-index", exact=True),
                ],
            }
        )

    dashboards = dashboard_items(request)

    if dashboards:
        sections.append({"title": "Dashboard", "items": dashboards})

    if access.is_admin(user):
        sections.append(
            {
                "title": "Administration",
                "items": [
                    _item(request, "All tickets", "ticket", "support:manage-tickets"),
                    _item(request, "Departments", "building", "support:manage-departments"),
                    _item(request, "Applications", "grid", "support:manage-applications"),
                    _item(request, "Teams and members", "users", "support:manage-teams"),
                    _item(request, "People and roles", "shield", "support:manage-people"),
                    _item(request, "Reports and analytics", "chart", "support:manage-reports"),
                    _item(request, "Activity history", "activity", "support:manage-activity"),
                    _item(request, "Settings", "settings", "support:manage-settings"),
                ],
            }
        )

    return sections, unread


def dashboard_items(request):
    """The JI dashboards (boards app), for those who may see them."""

    from django.apps import apps

    if not apps.is_installed("boards"):
        return []

    from boards import registry
    from boards.access import can_view_dashboards

    if not can_view_dashboards(request.user):
        return []

    items = [_item(request, "All dashboards", "grid", "boards:index", exact=True)]
    items += [_item(request, slide.label, slide.icon, "boards:board", args=[slide.key]) for slide in registry.SLIDES]
    items.append(_item(request, "Wall carousel", "monitor", "boards:carousel"))

    return items


def portal_context(request, title="", **extra):
    nav, unread = build_nav(request)
    user = request.user

    return {
        "nav_sections": nav,
        "unread_notifications": unread,
        "role_label": access.role_label(user),
        "is_support_admin": access.is_admin(user),
        "show_auth_admin": user.is_staff,
        "environment": "Development" if settings.DEBUG else "",
        "title": title,
        **extra,
    }
