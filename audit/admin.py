import csv
import json

from django.contrib import admin
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from adminpanel.admin_tools import badge, recent_filter, relative_time, titled

from .models import AuditEvent


class SeverityFilter(admin.SimpleListFilter):
    title = "severity"
    parameter_name = "severity"

    def lookups(self, request, model_admin):
        return AuditEvent.Severity.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(type__in=AuditEvent.types_with_severity(self.value()))

        return queryset


class Echo:
    """A file-like object csv.writer can write to, for streaming."""

    def write(self, value):
        return value


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):

    list_display = [
        "when",
        "event",
        "subject",
        "actor_email",
        "application",
        "ip_address",
        "summary",
    ]

    list_display_links = ["when"]

    list_filter = [
        ("type", titled("event", admin.ChoicesFieldListFilter)),
        SeverityFilter,
        recent_filter("created_at", "time", "when"),
        ("application", titled("application", admin.RelatedFieldListFilter, empty=False)),
    ]

    search_fields = [
        "email",
        "ip_address",
        "actor__email",
    ]

    date_hierarchy = "created_at"

    list_select_related = [
        "user",
        "actor",
        "application",
    ]

    list_per_page = 50

    fields = [
        "created_at",
        "type",
        "email",
        "user",
        "actor",
        "application",
        "ip_address",
        "user_agent",
        "pretty_details",
    ]

    readonly_fields = fields

    actions = [
        "export_csv",
    ]

    # The log is written by the service and never edited by hand.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    # ---- Columns ----------------------------------------------------------

    @admin.display(description="When", ordering="created_at")
    def when(self, event):
        return relative_time(event.created_at)

    @admin.display(description="Event", ordering="type")
    def event(self, event):
        tone = {"warning": "warning", "critical": "critical"}.get(event.severity, "neutral")
        return badge(event.get_type_display(), tone)

    @admin.display(description="Account", ordering="email")
    def subject(self, event):
        return event.email or "-"

    @admin.display(description="By", ordering="actor__email")
    def actor_email(self, event):
        return event.actor.email if event.actor else mark_safe('<span class="jv-muted">-</span>')

    @admin.display(description="Details")
    def pretty_details(self, event):
        return format_html(
            "<pre class='jv-pre'>{}</pre>",
            json.dumps(event.details, indent=2, sort_keys=True) if event.details else "{}",
        )

    # ---- Export -----------------------------------------------------------

    @admin.action(description="Export selected events as CSV", permissions=["view"])
    def export_csv(self, request, queryset):
        writer = csv.writer(Echo())

        def rows():
            yield writer.writerow(
                ["time", "event", "severity", "account", "by", "application", "ip", "user_agent", "details"]
            )

            for event in queryset.select_related("actor", "application").iterator():
                yield writer.writerow(
                    [
                        event.created_at.isoformat(),
                        event.type,
                        event.severity,
                        event.email,
                        event.actor.email if event.actor else "",
                        event.application.slug if event.application else "",
                        event.ip_address or "",
                        event.user_agent,
                        json.dumps(event.details, sort_keys=True),
                    ]
                )

        filename = f"audit-log-{timezone.now():%Y%m%d-%H%M}.csv"

        response = StreamingHttpResponse(rows(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        return response
