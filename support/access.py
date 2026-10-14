"""
Who may do what in the ticket system. Every view, service and API endpoint
checks here, so the rules are the same everywhere and a URL typed by hand
gets the same answer as a button.

Roles (one person may hold several):

* **User**: any signed-in account. Raises tickets and follows their own.
* **Team member**: belongs to an active team. Handles the tickets routed to
  that team: claims, replies, internal notes, status, resolves.
* **Team manager**: a member with the manager role. Also always assigns,
  changes priorities, reassigns a member's tickets and sees the team's
  statistics.
* **Administrator**: a SupportAdministrator, or any superuser. Everything,
  on every ticket, plus the management pages at /manage/.

None of this uses Django's permission system or the admin panel.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from .models import SupportAdministrator, SupportSettings, TeamMembership, Ticket


def _signed_in(user):
    return getattr(user, "is_authenticated", False) and getattr(user, "is_active", False)


def is_admin(user):
    if not _signed_in(user):
        return False

    if user.is_superuser:
        return True

    cached = getattr(user, "_support_is_admin", None)

    if cached is None:
        cached = SupportAdministrator.objects.filter(user=user).exists()
        user._support_is_admin = cached

    return cached


def memberships(user):
    """{team_id: role} for the user's active teams, cached on the user."""

    if not _signed_in(user):
        return {}

    cached = getattr(user, "_support_memberships", None)

    if cached is None:
        cached = dict(
            TeamMembership.objects
            .filter(user=user, team__is_active=True)
            .values_list("team_id", "role")
        )
        user._support_memberships = cached

    return cached


def forget(user):
    """Drop the cached roles, after they changed."""

    for name in ("_support_is_admin", "_support_memberships"):
        if hasattr(user, name):
            delattr(user, name)


def team_ids(user):
    return set(memberships(user))


def managed_team_ids(user):
    return {team_id for team_id, role in memberships(user).items() if role == TeamMembership.Role.MANAGER}


def is_team_member(user):
    return bool(memberships(user))


def is_manager(user, team_id=None):
    if team_id is None:
        return bool(managed_team_ids(user))

    return memberships(user).get(team_id) == TeamMembership.Role.MANAGER


def role_label(user):
    if getattr(user, "is_superuser", False):
        return "Superuser"

    if is_admin(user):
        return "Administrator"

    if is_manager(user):
        return "Team manager"

    if is_team_member(user):
        return "Team member"

    return "User"


# ---- Teams ---------------------------------------------------------------

def can_view_team(user, team_id):
    return is_admin(user) or team_id in team_ids(user)


def can_manage_team(user, team_id):
    """Reassign the team's tickets and see its detailed statistics."""

    return is_admin(user) or is_manager(user, team_id)


def can_edit_team(user):
    """Create teams, change their members and roles: administrators only."""

    return is_admin(user)


# ---- Tickets ---------------------------------------------------------------

def visible_tickets(user):
    """Tickets the user may open: all for administrators, otherwise their
    own and their teams'."""

    if is_admin(user):
        return Ticket.objects.all()

    if not _signed_in(user):
        return Ticket.objects.none()

    return Ticket.objects.filter(Q(created_by=user) | Q(team_id__in=team_ids(user)))


def can_view(user, ticket):
    if not _signed_in(user):
        return False

    if is_admin(user):
        return True

    return ticket.created_by_id == user.pk or ticket.team_id in team_ids(user)


def can_handle(user, ticket):
    """Act as the responsible team: internal notes, claim, status, resolve, ..."""

    return is_admin(user) or (ticket.team_id is not None and ticket.team_id in team_ids(user))


def can_view_internal(user, ticket):
    return can_handle(user, ticket)


def is_creator(user, ticket):
    return ticket.created_by_id == user.pk


def can_comment(user, ticket):
    if not can_view(user, ticket):
        return False

    # A closed ticket is reopened before the conversation goes on, except
    # by the team, which may still leave a word on it.
    return ticket.status != Ticket.Status.CLOSED or can_handle(user, ticket)


def can_claim(user, ticket):
    if not ticket.is_active or ticket.assignee_id is not None:
        return False

    if is_admin(user):
        return True

    return ticket.team_id is not None and ticket.team_id in team_ids(user)


def _team_permission(user, ticket, setting, config):
    if not ticket.is_active:
        return False

    if is_admin(user):
        return True

    if ticket.team_id not in team_ids(user):
        return False

    if is_manager(user, ticket.team_id):
        return True

    config = config or SupportSettings.load()

    return getattr(config, setting)


def can_assign(user, ticket, config=None):
    """Assign the ticket to a member of its team (or unassign it)."""

    return _team_permission(user, ticket, "members_can_assign", config)


def can_change_priority(user, ticket, config=None):
    return _team_permission(user, ticket, "members_can_change_priority", config)


def can_change_team(user, ticket):
    return is_admin(user) and ticket.is_active


def can_request_info(user, ticket):
    return ticket.is_active and can_handle(user, ticket)


def can_resolve(user, ticket):
    return ticket.is_active and can_handle(user, ticket)


def can_change_status(user, ticket):
    return ticket.is_active and can_handle(user, ticket)


def can_close(user, ticket):
    """Teams and administrators close from any active state; the person who
    raised the ticket may close it once it's resolved."""

    if ticket.status == Ticket.Status.CLOSED:
        return False

    if can_handle(user, ticket):
        return True

    return ticket.status == Ticket.Status.RESOLVED and is_creator(user, ticket)


def can_reopen(user, ticket, config=None):
    if not ticket.is_done:
        return False

    if can_handle(user, ticket):
        return True

    if not is_creator(user, ticket):
        return False

    config = config or SupportSettings.load()

    if not config.users_can_reopen:
        return False

    if config.reopen_window_days and ticket.done_at:
        return timezone.now() <= ticket.done_at + timedelta(days=config.reopen_window_days)

    return True


def assignable_users(ticket):
    """Active members of the ticket's team, for assignment pickers."""

    User = get_user_model()

    if ticket.team_id is None:
        return User.objects.none()

    return team_member_users(ticket.team_id)


def team_member_users(team_id):
    return (
        get_user_model().objects
        .filter(is_active=True, team_memberships__team_id=team_id)
        .order_by("first_name", "last_name", "email")
        .distinct()
    )
