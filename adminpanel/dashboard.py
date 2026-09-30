"""
Figures for the admin dashboard. Each section is only computed for staff
who may view the underlying model, so nobody sees numbers they couldn't
open.
"""

from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

from applications.models import Application
from audit.models import AuditEvent
from authentication.models import UserSession
from users.models import User

from .charts import Series, bar_list, column_chart


CHART_DAYS = 14


def changelist(model_label, query=""):
    app_label, model_name = model_label.split(".")
    url = reverse(f"admin:{app_label}_{model_name}_changelist")

    return f"{url}?{query}" if query else url


def delta(current, previous, up_is_good=True):
    difference = current - previous

    if difference == 0:
        return {"text": "No change", "direction": "flat", "tone": "neutral"}

    rising = difference > 0

    return {
        "text": f"{difference:+,}",
        "direction": "up" if rising else "down",
        "tone": "good" if rising == up_is_good else "bad",
    }


def daily_counts(queryset, field, start):
    rows = (
        queryset
        .filter(**{f"{field}__date__gte": start})
        .annotate(day=TruncDate(field))
        .values("day")
        .annotate(count=Count("pk"))
    )

    return {row["day"]: row["count"] for row in rows}


def user_section(now, today, days):
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    counts = User.objects.aggregate(
        total=Count("pk"),
        active=Count("pk", filter=Q(is_active=True)),
        unverified=Count("pk", filter=Q(is_verified=False, is_active=True)),
        stale_unverified=Count(
            "pk",
            filter=Q(is_verified=False, is_active=True, created_at__lt=week_ago),
        ),
        new_week=Count("pk", filter=Q(created_at__gte=week_ago)),
        new_prior_week=Count(
            "pk",
            filter=Q(created_at__gte=two_weeks_ago, created_at__lt=week_ago),
        ),
    )

    # Separate query: joining the grants would count users once per grant.
    no_access = User.objects.filter(
        is_active=True,
        applications__isnull=True,
    ).count()

    return {
        "tiles": [
            {
                "label": "Users",
                "value": counts["total"],
                "note": f"{counts['active']:,} active",
                "url": changelist("users.user"),
                "icon": "users",
            },
            {
                "label": "New users, last 7 days",
                "value": counts["new_week"],
                "delta": delta(counts["new_week"], counts["new_prior_week"]),
                "delta_label": "vs previous 7 days",
                "url": changelist("users.user", "joined=7d"),
                "icon": "user-plus",
            },
        ],
        "chart": column_chart(
            days,
            [
                Series(
                    "series-1",
                    "New users",
                    daily_counts(User.objects.all(), "created_at", days[0]),
                ),
            ],
            today,
        ),
        "attention": [
            {
                "count": counts["stale_unverified"],
                "text": "unverified for over a week",
                "hint": "They can't sign in until they verify their email.",
                "url": changelist("users.user", "is_verified__exact=0&is_active__exact=1"),
                "tone": "warning",
            },
            {
                "count": no_access,
                "text": "active users without application access",
                "hint": "They can sign in, but no application accepts them.",
                "url": changelist("users.user", "has_access=no"),
                "tone": "info",
            },
        ],
    }


def session_section(now):
    refresh_lifetime = settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]

    live = UserSession.objects.filter(
        revoked_at__isnull=True,
        last_used_at__gte=now - refresh_lifetime,
    )

    counts = live.aggregate(
        sessions=Count("pk"),
        users_today=Count(
            "user",
            distinct=True,
            filter=Q(last_used_at__gte=now - timedelta(days=1)),
        ),
    )

    return {
        "tiles": [
            {
                "label": "Active sessions",
                "value": counts["sessions"],
                "note": f"{counts['users_today']:,} users active today",
                "url": changelist("authentication.usersession", "status=active"),
                "icon": "monitor",
            },
        ],
    }


def signin_section(now, today, days):
    day_ago = now - timedelta(days=1)
    two_days_ago = now - timedelta(days=2)

    signins = AuditEvent.objects.filter(
        type__in=[AuditEvent.Type.LOGIN_SUCCEEDED, AuditEvent.Type.LOGIN_FAILED],
    )

    counts = signins.aggregate(
        ok_today=Count(
            "pk",
            filter=Q(type=AuditEvent.Type.LOGIN_SUCCEEDED, created_at__gte=day_ago),
        ),
        ok_before=Count(
            "pk",
            filter=Q(
                type=AuditEvent.Type.LOGIN_SUCCEEDED,
                created_at__gte=two_days_ago,
                created_at__lt=day_ago,
            ),
        ),
        failed_today=Count(
            "pk",
            filter=Q(type=AuditEvent.Type.LOGIN_FAILED, created_at__gte=day_ago),
        ),
        failed_before=Count(
            "pk",
            filter=Q(
                type=AuditEvent.Type.LOGIN_FAILED,
                created_at__gte=two_days_ago,
                created_at__lt=day_ago,
            ),
        ),
    )

    by_day = {AuditEvent.Type.LOGIN_SUCCEEDED: {}, AuditEvent.Type.LOGIN_FAILED: {}}

    for row in (
        signins
        .filter(created_at__date__gte=days[0])
        .annotate(day=TruncDate("created_at"))
        .values("day", "type")
        .annotate(count=Count("pk"))
    ):
        by_day[row["type"]][row["day"]] = row["count"]

    failed_ips = (
        AuditEvent.objects
        .filter(
            type=AuditEvent.Type.LOGIN_FAILED,
            created_at__gte=day_ago,
            ip_address__isnull=False,
        )
        .values("ip_address")
        .annotate(
            count=Count("pk"),
            accounts=Count("email", distinct=True),
        )
        .filter(count__gte=3)
        .order_by("-count")[:5]
    )

    reuse = AuditEvent.objects.filter(
        type=AuditEvent.Type.TOKEN_REUSED,
        created_at__gte=now - timedelta(days=7),
    ).count()

    return {
        "tiles": [
            {
                "label": "Sign-ins, last 24 hours",
                "value": counts["ok_today"],
                "delta": delta(counts["ok_today"], counts["ok_before"]),
                "delta_label": "vs the 24 hours before",
                "url": changelist("audit.auditevent", "type__exact=login_succeeded"),
                "icon": "log-in",
            },
            {
                "label": "Failed sign-ins, last 24 hours",
                "value": counts["failed_today"],
                "delta": delta(
                    counts["failed_today"],
                    counts["failed_before"],
                    up_is_good=False,
                ),
                "delta_label": "vs the 24 hours before",
                "url": changelist("audit.auditevent", "type__exact=login_failed"),
                "icon": "alert",
            },
        ],
        "chart": column_chart(
            days,
            [
                Series("series-1", "Successful", by_day[AuditEvent.Type.LOGIN_SUCCEEDED]),
                Series("series-2", "Failed", by_day[AuditEvent.Type.LOGIN_FAILED]),
            ],
            today,
        ),
        "failed_ips": [
            {
                **row,
                "url": changelist(
                    "audit.auditevent",
                    f"type__exact=login_failed&ip_address={row['ip_address']}",
                ),
            }
            for row in failed_ips
        ],
        "attention": [
            {
                "count": reuse,
                "text": "refresh token reuse alerts in the last 7 days",
                "hint": "A reused token may mean it was stolen; the session was revoked.",
                "url": changelist("audit.auditevent", "type__exact=token_reused"),
                "tone": "critical",
            },
        ],
        "activity": list(
            AuditEvent.objects
            .select_related("user", "actor", "application")[:10]
        ),
    }


def application_section():
    applications = list(
        Application.objects
        .filter(is_active=True)
        .annotate(user_count=Count("users", filter=Q(users__is_active=True)))
    )

    without_key = sum(1 for app in applications if not app.api_key_hash)

    return {
        "tiles": [
            {
                "label": "Applications",
                "value": len(applications),
                "note": (
                    f"{without_key} without an API key"
                    if without_key
                    else "All have an API key"
                ),
                "url": changelist("applications.application", "is_active__exact=1"),
                "icon": "grid",
            },
        ],
        "bars": bar_list(
            [
                (
                    app.name,
                    app.user_count,
                    reverse("admin:applications_application_change", args=[app.pk]),
                )
                for app in applications
            ]
        ),
    }


def build_dashboard(request):
    user = request.user
    now = timezone.now()
    today = timezone.localdate(now)
    days = [today - timedelta(days=offset) for offset in range(CHART_DAYS - 1, -1, -1)]

    dashboard = {"tiles": [], "attention": []}

    if user.has_perm("users.view_user"):
        users = user_section(now, today, days)
        dashboard["tiles"] += users["tiles"]
        dashboard["signups"] = users["chart"]
        dashboard["attention"] += users["attention"]

    if user.has_perm("authentication.view_usersession"):
        dashboard["tiles"] += session_section(now)["tiles"]

    if user.has_perm("audit.view_auditevent"):
        signins = signin_section(now, today, days)
        dashboard["tiles"] += signins["tiles"]
        dashboard["signins"] = signins["chart"]
        dashboard["failed_ips"] = signins["failed_ips"]
        dashboard["attention"] += signins["attention"]
        dashboard["activity"] = signins["activity"]

    if user.has_perm("applications.view_application"):
        applications = application_section()
        dashboard["tiles"] += applications["tiles"]
        dashboard["applications"] = applications["bars"]

    dashboard["attention"] = [item for item in dashboard["attention"] if item["count"]]

    return dashboard
