"""Building blocks shared by the ModelAdmins: badges, times, filters, confirmations."""

from datetime import timedelta

from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.timesince import timesince


def badge(text, tone="neutral"):
    """tone: neutral, info, good, warning, critical."""

    return format_html('<span class="jv-badge jv-badge--{}">{}</span>', tone, text)


def relative_time(moment, empty="Never"):
    if moment is None:
        return format_html('<span class="jv-muted">{}</span>', empty)

    now = timezone.now()
    words = "just now" if now - moment < timedelta(minutes=1) else f"{timesince(moment, now).split(',')[0]} ago"

    return format_html(
        '<time datetime="{}" title="{}">{}</time>',
        moment.isoformat(),
        timezone.localtime(moment).strftime("%Y-%m-%d %H:%M %Z"),
        words,
    )


BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)

SYSTEMS = (
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("Windows", "Windows"),
    ("Mac OS X", "macOS"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)


def describe_user_agent(user_agent):
    """"Chrome on Windows", or the client's first token (e.g. an SDK)."""

    if not user_agent:
        return ""

    browser = next((name for token, name in BROWSERS if token in user_agent), None)
    system = next((name for token, name in SYSTEMS if token in user_agent), None)

    if browser and system:
        return f"{browser} on {system}"

    return browser or system or user_agent.split(" ")[0][:40]


def titled(title, base=admin.BooleanFieldListFilter, empty=True):
    """A field list filter with a readable title (not "By is active")."""

    class TitledFilter(base):

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.title = title

        if not empty:
            # The "-" (no value) choice; a dedicated filter covers it.
            include_empty_choice = False

    return TitledFilter


def recent_filter(field, title, parameter, never_label=None):
    """A list filter for a datetime field: today, 7/30/90 days, older."""

    class RecentFilter(admin.SimpleListFilter):

        def lookups(self, request, model_admin):
            choices = [
                ("today", "Today"),
                ("7d", "Last 7 days"),
                ("30d", "Last 30 days"),
                ("90d", "Last 90 days"),
                ("older", "More than 90 days ago"),
            ]

            if never_label:
                choices.append(("never", never_label))

            return choices

        def queryset(self, request, queryset):
            value = self.value()
            now = timezone.now()

            if value == "today":
                return queryset.filter(**{f"{field}__gte": now - timedelta(days=1)})

            if value in ("7d", "30d", "90d"):
                days = int(value[:-1])
                return queryset.filter(**{f"{field}__gte": now - timedelta(days=days)})

            if value == "older":
                return queryset.filter(**{f"{field}__lt": now - timedelta(days=90)})

            if value == "never":
                return queryset.filter(**{f"{field}__isnull": True})

            return queryset

    RecentFilter.title = title
    RecentFilter.parameter_name = parameter
    RecentFilter.__name__ = f"{parameter.title()}Filter"

    return RecentFilter


def confirm_action(
    modeladmin,
    request,
    queryset,
    *,
    title,
    message,
    confirm_label,
    perform,
    danger=False,
    extra_form=None,
):
    """
    Ask before a bulk action, like Django's delete confirmation. On the
    confirming POST, runs perform(queryset, form) and returns None so the
    admin redirects back to the list; otherwise returns the confirmation page.
    """

    if request.POST.get("jv_confirm") == "yes":
        form = extra_form(request.POST) if extra_form else None

        if form is None or form.is_valid():
            result = perform(queryset, form)

            if result:
                modeladmin.message_user(request, result, messages.SUCCESS)

            return None
    else:
        form = extra_form() if extra_form else None

    opts = modeladmin.model._meta

    return TemplateResponse(
        request,
        "adminpanel/confirm_action.html",
        {
            **modeladmin.admin_site.each_context(request),
            "title": title,
            "message": message,
            "confirm_label": confirm_label,
            "danger": danger,
            "form": form,
            "objects": queryset[:50],
            "count": queryset.count(),
            "opts": opts,
            "action": request.POST.get("action", ""),
            "selected": request.POST.getlist(helpers.ACTION_CHECKBOX_NAME),
            "select_across": request.POST.get("select_across", "0"),
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
        },
    )
