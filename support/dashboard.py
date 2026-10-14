"""
Figures for the ticket system's dashboard at /, one section per role the
signed-in person holds: their own tickets, their teams' queue, their
managed teams' performance, and (administrators) the whole system. Every
number is counted in the database when the page loads.
"""

from datetime import timedelta
from urllib.parse import urlencode

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q
from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

from adminpanel.charts import Series, bar_list, column_chart
from applications.models import Application

from . import access
from .models import Department, Team, TeamMembership, Ticket, TicketEvent


CHART_DAYS = 14
PERFORMANCE_DAYS = 30

Status = Ticket.Status


def _url(name, query=None, args=()):
    url = reverse(name, args=args)
    query = {key: value for key, value in (query or {}).items() if value not in (None, "")}

    return f"{url}?{urlencode(query)}" if query else url


def manage_url(**query):
    return _url("support:manage-tickets", query)


def team_url(preset, **query):
    return _url("support:team-tickets", query, args=[preset])


def my_url(**query):
    return _url("support:my-tickets", query)


def overdue_q(now, prefix=""):
    return Q(**{f"{prefix}status__in": Ticket.ACTIVE_STATUSES}) & (
        Q(**{f"{prefix}resolve_due_at__lt": now})
        | Q(**{f"{prefix}first_response_at__isnull": True, f"{prefix}response_due_at__lt": now})
    )


def status_counts(queryset, now=None):
    """Counts by workflow state, in one query."""

    now = now or timezone.now()

    return queryset.aggregate(
        total=Count("pk"),
        open=Count("pk", filter=Q(status__in=Ticket.OPEN_STATUSES)),
        unassigned=Count("pk", filter=Q(status__in=Ticket.ACTIVE_STATUSES, assignee__isnull=True)),
        in_progress=Count("pk", filter=Q(status=Status.IN_PROGRESS)),
        waiting=Count("pk", filter=Q(status=Status.WAITING)),
        resolved=Count("pk", filter=Q(status=Status.RESOLVED)),
        closed=Count("pk", filter=Q(status=Status.CLOSED)),
        active=Count("pk", filter=Q(status__in=Ticket.ACTIVE_STATUSES)),
        overdue=Count("pk", filter=overdue_q(now)),
        unrouted=Count("pk", filter=Q(status__in=Ticket.ACTIVE_STATUSES, team__isnull=True)),
    )


def daily_counts(queryset, field, start):
    rows = (
        queryset
        .filter(**{f"{field}__date__gte": start})
        .annotate(day=TruncDate(field))
        .values("day")
        .annotate(count=Count("pk"))
    )

    return {row["day"]: row["count"] for row in rows}


def average_hours(queryset, end_field, start_field="created_at"):
    """Mean hours between two timestamps, over rows where both are set."""

    duration = ExpressionWrapper(F(end_field) - F(start_field), output_field=DurationField())
    result = queryset.filter(**{f"{end_field}__isnull": False}).aggregate(mean=Avg(duration), count=Count("pk"))

    if not result["mean"]:
        return None, result["count"]

    return result["mean"].total_seconds() / 3600, result["count"]


def describe_hours(hours):
    if hours is None:
        return "—"

    if hours < 1:
        return f"{hours * 60:.0f} min"

    if hours < 48:
        return f"{hours:.1f} h".replace(".0 h", " h")

    return f"{hours / 24:.1f} days".replace(".0 days", " days")


def performance(tickets, now, days=PERFORMANCE_DAYS):
    """Created, resolved, response and resolution times, SLA share."""

    since = now - timedelta(days=days)
    created = tickets.filter(created_at__gte=since)
    resolved = tickets.filter(resolved_at__gte=since)

    response_hours, response_n = average_hours(created, "first_response_at")
    resolution_hours, resolution_n = average_hours(resolved, "resolved_at")
    sla = resolved.filter(resolve_due_at__isnull=False).aggregate(
        total=Count("pk"),
        met=Count("pk", filter=Q(resolved_at__lte=F("resolve_due_at"))),
    )

    return {
        "days": days,
        "created": created.count(),
        "resolved": resolved.count(),
        "response": describe_hours(response_hours),
        "response_count": response_n,
        "resolution": describe_hours(resolution_hours),
        "resolution_count": resolution_n,
        "sla_percent": round(sla["met"] / sla["total"] * 100) if sla["total"] else None,
        "sla_total": sla["total"],
    }


def team_workload(teams, now=None):
    """Per team: how many tickets in each state."""

    now = now or timezone.now()

    # distinct: the memberships join repeats every ticket once per member.
    rows = list(
        teams.annotate(
            open_count=Count("tickets", filter=Q(tickets__status__in=Ticket.OPEN_STATUSES), distinct=True),
            unassigned_count=Count("tickets", filter=Q(tickets__status__in=Ticket.ACTIVE_STATUSES, tickets__assignee__isnull=True), distinct=True),
            in_progress_count=Count("tickets", filter=Q(tickets__status=Status.IN_PROGRESS), distinct=True),
            waiting_count=Count("tickets", filter=Q(tickets__status=Status.WAITING), distinct=True),
            resolved_count=Count("tickets", filter=Q(tickets__status=Status.RESOLVED), distinct=True),
            closed_count=Count("tickets", filter=Q(tickets__status=Status.CLOSED), distinct=True),
            overdue_count=Count("tickets", filter=overdue_q(now, "tickets__"), distinct=True),
            member_count=Count("memberships", distinct=True),
        ).order_by("name")
    )

    for team in rows:
        team.active_count = team.open_count + team.in_progress_count + team.waiting_count
        team.url = reverse("support:team-overview", args=[team.pk])

    return rows


def member_workload(team_ids):
    """Per team member: active tickets of that team assigned, waiting, resolved."""

    memberships = list(
        TeamMembership.objects
        .filter(team_id__in=team_ids)
        .select_related("user", "team")
        .order_by("team__name", "user__first_name", "user__email")
    )

    counts = {
        (row["team"], row["assignee"]): row
        for row in (
            Ticket.objects
            .filter(team_id__in=team_ids, assignee_id__in={m.user_id for m in memberships})
            .values("team", "assignee")
            .annotate(
                active=Count("pk", filter=Q(status__in=Ticket.ACTIVE_STATUSES)),
                waiting=Count("pk", filter=Q(status=Status.WAITING)),
                resolved=Count("pk", filter=Q(status__in=Ticket.DONE_STATUSES)),
            )
        )
    }

    return [
        {
            "membership": membership,
            "user": membership.user,
            "team": membership.team,
            "active": counts.get((membership.team_id, membership.user_id), {}).get("active", 0),
            "waiting": counts.get((membership.team_id, membership.user_id), {}).get("waiting", 0),
            "resolved": counts.get((membership.team_id, membership.user_id), {}).get("resolved", 0),
        }
        for membership in memberships
    ]


# ---- Sections -------------------------------------------------------------------

def user_section(user):
    mine = Ticket.objects.filter(created_by=user)
    counts = mine.aggregate(
        total=Count("pk"),
        active=Count("pk", filter=Q(status__in=Ticket.ACTIVE_STATUSES)),
        waiting=Count("pk", filter=Q(status=Status.WAITING)),
        resolved=Count("pk", filter=Q(status=Status.RESOLVED)),
        closed=Count("pk", filter=Q(status=Status.CLOSED)),
    )

    return {
        "counts": counts,
        "tiles": [
            {"label": "My open tickets", "value": counts["active"], "icon": "ticket", "url": my_url(status="active")},
            {"label": "Waiting for my reply", "value": counts["waiting"], "icon": "help", "url": my_url(status=Status.WAITING), "tone": "warning" if counts["waiting"] else ""},
            {"label": "Resolved, to confirm", "value": counts["resolved"], "icon": "check-circle", "url": my_url(status=Status.RESOLVED)},
            {"label": "Closed", "value": counts["closed"], "icon": "archive", "url": my_url(status=Status.CLOSED)},
        ],
        "recent": list(mine.with_related().order_by("-updated_at")[:6]),
    }


def team_section(user, now):
    team_ids = access.team_ids(user)
    tickets = Ticket.objects.filter(team_id__in=team_ids)
    counts = status_counts(tickets, now)
    mine = Ticket.objects.filter(assignee=user).aggregate(
        active=Count("pk", filter=Q(status__in=Ticket.ACTIVE_STATUSES)),
        overdue=Count("pk", filter=overdue_q(now)),
    )

    return {
        "teams": list(Team.objects.filter(pk__in=team_ids).order_by("name")),
        "counts": counts,
        "tiles": [
            {"label": "New for my teams", "value": counts["open"], "icon": "inbox", "url": team_url("all", status="new"), "note": ""},
            {"label": "Available to claim", "value": counts["unassigned"], "icon": "user-x", "url": team_url("available"), "note": ""},
            {"label": "Assigned to me", "value": mine["active"], "icon": "user-check", "url": team_url("mine"), "note": f"{mine['overdue']} overdue" if mine["overdue"] else "", "tone": "critical" if mine["overdue"] else ""},
            {"label": "In progress", "value": counts["in_progress"], "icon": "activity", "url": team_url("all", status=Status.IN_PROGRESS)},
            {"label": "Waiting for information", "value": counts["waiting"], "icon": "clock", "url": team_url("all", status=Status.WAITING)},
            {"label": "Resolved and closed", "value": counts["resolved"] + counts["closed"], "icon": "check-circle", "url": team_url("resolved")},
        ],
        "available": list(tickets.unassigned().with_related().order_by("created_at")[:6]),
        "assigned": list(Ticket.objects.filter(assignee=user).active().with_related().order_by("-updated_at")[:6]),
        "overdue": list(tickets.overdue(now).with_related().order_by(F("resolve_due_at").asc(nulls_last=True))[:6]),
    }


def manager_section(user, now):
    team_ids = access.managed_team_ids(user)
    teams = Team.objects.filter(pk__in=team_ids)

    rows = []

    for team in team_workload(teams, now):
        team.performance = performance(Ticket.objects.filter(team=team), now)
        rows.append(team)

    return {
        "teams": rows,
        "members": member_workload(team_ids),
    }


def admin_section(now):
    today = timezone.localdate(now)
    days = [today - timedelta(days=offset) for offset in range(CHART_DAYS - 1, -1, -1)]
    week_ago = now - timedelta(days=7)

    tickets = Ticket.objects.all()
    counts = status_counts(tickets, now)

    tiles = [
        ("Total tickets", counts["total"], "", "ticket", manage_url()),
        ("New and open", counts["open"], "", "inbox", manage_url(status="new")),
        ("Unassigned", counts["unassigned"], "", "user-x", manage_url(status="active", assignee="none")),
        ("In progress", counts["in_progress"], "", "activity", manage_url(status=Status.IN_PROGRESS)),
        ("Waiting for information", counts["waiting"], "", "clock", manage_url(status=Status.WAITING)),
        ("Resolved", counts["resolved"], "", "check-circle", manage_url(status=Status.RESOLVED)),
        ("Closed", counts["closed"], "", "archive", manage_url(status=Status.CLOSED)),
        ("Overdue", counts["overdue"], "", "alert", manage_url(sla="overdue")),
    ]

    created = daily_counts(tickets, "created_at", days[0])
    resolved = daily_counts(tickets, "resolved_at", days[0])

    departments = list(
        Department.objects
        .annotate(pending=Count("tickets", filter=Q(tickets__status__in=Ticket.ACTIVE_STATUSES), distinct=True))
        .order_by("-pending", "name")
    )

    applications = list(
        Application.objects
        .filter(tickets__isnull=False)
        .annotate(pending=Count("tickets", filter=Q(tickets__status__in=Ticket.ACTIVE_STATUSES), distinct=True))
        .order_by("-pending", "name")
        .distinct()
    )

    stale_waiting = tickets.filter(status=Status.WAITING, updated_at__lt=week_ago).count()
    apps_without_team = Application.objects.filter(
        tickets_enabled=True, department__isnull=False, support_team__isnull=True,
    ).exclude(department__default_team__isnull=False).count()

    attention = [
        {
            "count": counts["overdue"],
            "text": "overdue tickets",
            "url": manage_url(sla="overdue"),
            "tone": "critical",
        },
        {
            "count": counts["unrouted"],
            "text": "active tickets without a team",
            "url": manage_url(status="active", team="none"),
            "tone": "warning",
        },
        {
            "count": stale_waiting,
            "text": "tickets waiting for the user for over a week",
            "url": manage_url(status=Status.WAITING, updated_to=timezone.localdate(week_ago).isoformat()),
            "tone": "info",
        },
        {
            "count": apps_without_team,
            "text": "applications whose tickets can't be routed",
            "url": _url("support:manage-applications", {"routing": "no_team"}),
            "tone": "warning",
        },
    ]

    return {
        "tiles": [
            {"label": label, "value": value, "note": note, "icon": icon, "url": url}
            for label, value, note, icon, url in tiles
        ],
        "chart": column_chart(
            days,
            [Series("series-1", "Created", created), Series("series-2", "Resolved", resolved)],
            today,
        ),
        "chart_total": sum(created.values()),
        "departments": bar_list(
            [(d.name, d.pending, manage_url(department=d.pk, status="active")) for d in departments],
            limit=8,
        ),
        "applications": bar_list(
            [(a.name, a.pending, manage_url(application=a.pk, status="active")) for a in applications],
            limit=8,
        ),
        "teams": team_workload(Team.objects.filter(is_active=True), now),
        "performance": performance(tickets, now),
        "attention": [item for item in attention if item["count"]],
        "activity": list(TicketEvent.objects.select_related("ticket", "actor")[:10]),
    }


def build_dashboard(user):
    now = timezone.now()
    dashboard = {"now": now, "user": user_section(user)}

    if access.is_team_member(user):
        dashboard["team"] = team_section(user, now)

    if access.is_manager(user):
        dashboard["manager"] = manager_section(user, now)

    if access.is_admin(user):
        dashboard["admin"] = admin_section(now)

    return dashboard


def team_overview(team, user):
    """One team's page: counts, performance, members, applications."""

    now = timezone.now()
    tickets = Ticket.objects.filter(team=team)

    return {
        "team": team,
        "counts": status_counts(tickets, now),
        "performance": performance(tickets, now),
        "members": member_workload({team.pk}),
        "applications": list(team.applications.select_related("department").order_by("name")),
        "overdue": list(tickets.overdue(now).with_related().order_by(F("resolve_due_at").asc(nulls_last=True))[:10]),
        "now": now,
    }
