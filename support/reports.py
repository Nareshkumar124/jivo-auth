"""
Reports and analytics, from the tickets themselves. Filtered by a date
range (ticket creation), department, application and team; exported as
CSV with the same filters.
"""

import csv
from datetime import datetime, time, timedelta

from django import forms
from django.db.models import Count, F, Q
from django.db.models.functions import TruncDate
from django.http import StreamingHttpResponse
from django.utils import timezone

from adminpanel.charts import Series, bar_list, column_chart
from applications.models import Application

from .dashboard import average_hours, describe_hours
from .models import Department, Team, TeamMembership, Ticket


Status = Ticket.Status

MAX_CHART_DAYS = 92


class ReportFilterForm(forms.Form):

    start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    department = forms.ModelChoiceField(required=False, queryset=Department.objects.order_by("name"), empty_label="All departments")
    application = forms.ModelChoiceField(required=False, queryset=Application.objects.order_by("name"), empty_label="All applications")
    team = forms.ModelChoiceField(required=False, queryset=Team.objects.order_by("name"), empty_label="All teams")

    def clean(self):
        cleaned = super().clean()
        today = timezone.localdate()

        end = cleaned.get("end") or today
        start = cleaned.get("start") or end - timedelta(days=29)

        if start > end:
            start, end = end, start

        cleaned["start"], cleaned["end"] = start, end

        return cleaned


class Echo:

    def write(self, value):
        return value


def bounds(start, end):
    """Aware datetimes covering the days start..end inclusive."""

    zone = timezone.get_current_timezone()

    return (
        timezone.make_aware(datetime.combine(start, time.min), zone),
        timezone.make_aware(datetime.combine(end, time.max), zone),
    )


def filtered_tickets(data):
    tickets = Ticket.objects.all()

    if data.get("department"):
        tickets = tickets.filter(department=data["department"])

    if data.get("application"):
        tickets = tickets.filter(application=data["application"])

    if data.get("team"):
        tickets = tickets.filter(team=data["team"])

    return tickets


def grouped(queryset, field, label_field):
    rows = (
        queryset
        .values(field, label_field)
        .annotate(
            total=Count("pk"),
            active=Count("pk", filter=Q(status__in=Ticket.ACTIVE_STATUSES)),
            resolved=Count("pk", filter=Q(status__in=Ticket.DONE_STATUSES)),
        )
        .order_by("-total", label_field)
    )

    return [
        {"id": row[field], "label": row[label_field] or "—", "total": row["total"], "active": row["active"], "resolved": row["resolved"]}
        for row in rows
    ]


def choice_breakdown(queryset, field, choices):
    counts = dict(queryset.values_list(field).annotate(count=Count("pk")))
    total = sum(counts.values()) or 1

    return [
        {"value": value, "label": label, "count": counts.get(value, 0), "share": counts.get(value, 0) / total * 100}
        for value, label in choices
    ]


def pending_trend(tickets, days):
    """For each day: tickets still open at the end of it, and how many of
    those were overdue. Computed from the tickets themselves."""

    first, last = bounds(days[0], days[-1])

    rows = list(
        tickets
        .filter(created_at__lte=last)
        .filter(Q(resolved_at__isnull=True) | Q(resolved_at__gte=first))
        .values_list("created_at", "resolved_at", "closed_at", "resolve_due_at", "status")
    )

    pending = {}
    overdue = {}

    for day in days:
        _, end = bounds(day, day)
        open_count = 0
        late = 0

        for created_at, resolved_at, closed_at, due, status in rows:
            if created_at > end:
                continue

            finished = resolved_at or closed_at

            if finished is not None and finished <= end:
                continue

            # Active now but with no resolved_at (a reopened ticket) counts
            # as pending; a closed-without-resolution one stops at closed_at.
            open_count += 1

            if due is not None and due < end:
                late += 1

        pending[day] = open_count
        overdue[day] = late

    return pending, overdue


def build_report(data):
    start, end = data["start"], data["end"]
    first, last = bounds(start, end)
    today = timezone.localdate()

    tickets = filtered_tickets(data)
    created = tickets.filter(created_at__range=(first, last))
    resolved_in_range = tickets.filter(resolved_at__range=(first, last))

    span = (end - start).days + 1
    chart_days = None
    chart = None
    trend = None

    if span <= MAX_CHART_DAYS:
        chart_days = [start + timedelta(days=offset) for offset in range(span)]

        created_by_day = {
            row["day"]: row["count"]
            for row in created.annotate(day=TruncDate("created_at")).values("day").annotate(count=Count("pk"))
        }
        resolved_by_day = {
            row["day"]: row["count"]
            for row in resolved_in_range.annotate(day=TruncDate("resolved_at")).values("day").annotate(count=Count("pk"))
        }

        chart = column_chart(
            chart_days,
            [Series("series-1", "Created", created_by_day), Series("series-2", "Resolved", resolved_by_day)],
            today,
        )

        pending, overdue = pending_trend(tickets, chart_days)
        trend = column_chart(
            chart_days,
            [Series("series-1", "Pending", {day: pending[day] - overdue[day] for day in chart_days}), Series("series-2", "Overdue", overdue)],
            today,
        )

    response_hours, response_n = average_hours(created, "first_response_at")
    resolution_hours, resolution_n = average_hours(resolved_in_range, "resolved_at")

    within_sla = resolved_in_range.filter(resolve_due_at__isnull=False).aggregate(
        total=Count("pk"),
        met=Count("pk", filter=Q(resolved_at__lte=F("resolve_due_at"))),
    )

    summary = {
        "created": created.count(),
        "resolved": resolved_in_range.count(),
        "closed": tickets.filter(closed_at__range=(first, last)).count(),
        "reopened": tickets.filter(reopened_at__range=(first, last)).count(),
        "pending_now": tickets.active().count(),
        "overdue_now": tickets.overdue().count(),
        "response": {"hours": describe_hours(response_hours), "raw": response_hours, "count": response_n},
        "resolution": {"hours": describe_hours(resolution_hours), "raw": resolution_hours, "count": resolution_n},
        "sla": {
            "total": within_sla["total"],
            "met": within_sla["met"],
            "percent": (within_sla["met"] / within_sla["total"] * 100) if within_sla["total"] else None,
        },
    }

    departments = grouped(created, "department", "department__name")
    applications = grouped(created, "application", "application__name")
    teams = grouped(created, "team", "team__name")

    members = list(
        TeamMembership.objects
        .filter(**({"team": data["team"]} if data.get("team") else {}))
        .select_related("user", "team")
        .order_by("team__name", "user__first_name", "user__email")
    )
    user_ids = {membership.user_id for membership in members}
    assigned = {
        row["assignee"]: row
        for row in (
            tickets
            .filter(assignee_id__in=user_ids)
            .values("assignee")
            .annotate(
                active=Count("pk", filter=Q(status__in=Ticket.ACTIVE_STATUSES)),
                resolved=Count("pk", filter=Q(resolved_at__range=(first, last))),
            )
        )
    }
    member_rows = [
        {
            "user": membership.user,
            "team": membership.team,
            "role": membership.get_role_display(),
            "active": assigned.get(membership.user_id, {}).get("active", 0),
            "resolved": assigned.get(membership.user_id, {}).get("resolved", 0),
        }
        for membership in members
    ]

    return {
        "start": start,
        "end": end,
        "span": span,
        "chart": chart,
        "trend": trend,
        "chart_capped": span > MAX_CHART_DAYS,
        "summary": summary,
        "departments": departments,
        "department_bars": bar_list([(row["label"], row["total"], "") for row in departments], limit=10),
        "applications": applications,
        "application_bars": bar_list([(row["label"], row["total"], "") for row in applications], limit=10),
        "teams": teams,
        "statuses": choice_breakdown(created, "status", Status.choices),
        "priorities": choice_breakdown(created, "priority", Ticket.Priority.choices),
        "members": member_rows,
    }


# ---- CSV exports -----------------------------------------------------------

TICKET_COLUMNS = [
    "ticket", "subject", "status", "priority", "department", "application", "team",
    "assignee", "creator", "created_at", "updated_at", "first_response_at",
    "resolved_at", "closed_at", "response_due_at", "resolve_due_at", "reopened", "resolution",
]


def ticket_row(ticket):
    def when(value):
        return value.isoformat(timespec="seconds") if value else ""

    return [
        ticket.reference,
        ticket.subject,
        ticket.get_status_display(),
        ticket.get_priority_display(),
        ticket.department.name,
        ticket.application.name,
        ticket.team.name if ticket.team else "",
        ticket.assignee.email if ticket.assignee else "",
        ticket.created_by.email if ticket.created_by else "",
        when(ticket.created_at),
        when(ticket.updated_at),
        when(ticket.first_response_at),
        when(ticket.resolved_at),
        when(ticket.closed_at),
        when(ticket.response_due_at),
        when(ticket.resolve_due_at),
        ticket.reopen_count,
        ticket.resolution,
    ]


def csv_response(rows, filename):
    writer = csv.writer(Echo())

    def stream():
        # Excel reads UTF-8 CSV correctly only with a byte-order mark.
        yield "\ufeff"

        for row in rows:
            yield writer.writerow(row)

    response = StreamingHttpResponse(stream(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    return response


def export_tickets(queryset, filename=None):
    def rows():
        yield TICKET_COLUMNS

        for ticket in queryset.with_related().iterator(chunk_size=500):
            yield ticket_row(ticket)

    return csv_response(rows(), filename or f"tickets-{timezone.now():%Y%m%d-%H%M}.csv")


def export_summary(report):
    def rows():
        yield ["Support report", f"{report['start']} to {report['end']}"]
        yield []
        yield ["Metric", "Value"]
        summary = report["summary"]
        yield ["Tickets created", summary["created"]]
        yield ["Tickets resolved", summary["resolved"]]
        yield ["Tickets closed", summary["closed"]]
        yield ["Tickets reopened", summary["reopened"]]
        yield ["Pending now", summary["pending_now"]]
        yield ["Overdue now", summary["overdue_now"]]
        yield ["Average first response (hours)", f"{summary['response']['raw']:.2f}" if summary["response"]["raw"] is not None else ""]
        yield ["Average resolution (hours)", f"{summary['resolution']['raw']:.2f}" if summary["resolution"]["raw"] is not None else ""]
        yield ["Resolved within SLA (%)", f"{summary['sla']['percent']:.1f}" if summary["sla"]["percent"] is not None else ""]

        for title, key in (("Department", "departments"), ("Application", "applications"), ("Team", "teams")):
            yield []
            yield [title, "Created", "Active", "Resolved or closed"]
            for row in report[key]:
                yield [row["label"], row["total"], row["active"], row["resolved"]]

        for title, key in (("Status", "statuses"), ("Priority", "priorities")):
            yield []
            yield [title, "Tickets", "Share (%)"]
            for row in report[key]:
                yield [row["label"], row["count"], f"{row['share']:.1f}"]

        yield []
        yield ["Team member", "Team", "Role", "Active assigned", "Resolved in range"]
        for row in report["members"]:
            yield [row["user"].email, row["team"].name, row["role"], row["active"], row["resolved"]]

    return csv_response(rows(), f"support-report-{report['start']}-{report['end']}.csv")
