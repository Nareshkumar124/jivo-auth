"""Template tags of the ticket system's own templates."""

from django import template
from django.utils import timezone
from django.utils.html import format_html

from ..attachments import human_size as _human_size
from ..models import PRIORITY_TONES, STATUS_TONES, Ticket, TicketEvent


register = template.Library()

EVENT_ICONS = {
    TicketEvent.Type.CREATED: "plus",
    TicketEvent.Type.ASSIGNED: "user-check",
    TicketEvent.Type.UNASSIGNED: "user-x",
    TicketEvent.Type.STATUS_CHANGED: "refresh",
    TicketEvent.Type.PRIORITY_CHANGED: "flag",
    TicketEvent.Type.COMMENTED: "message",
    TicketEvent.Type.NOTE_ADDED: "eye-off",
    TicketEvent.Type.INFO_REQUESTED: "help",
    TicketEvent.Type.RESOLVED: "check-circle",
    TicketEvent.Type.CLOSED: "archive",
    TicketEvent.Type.REOPENED: "rotate",
    TicketEvent.Type.ATTACHMENT_ADDED: "paperclip",
    TicketEvent.Type.TEAM_CHANGED: "users",
}

EVENT_TONES = {
    TicketEvent.Type.CREATED: "info",
    TicketEvent.Type.RESOLVED: "good",
    TicketEvent.Type.REOPENED: "warning",
    TicketEvent.Type.INFO_REQUESTED: "warning",
}


@register.simple_tag
def icon(name, size=18, label=""):
    """An icon from support/partials/icons.html."""

    if label:
        return format_html(
            '<svg class="icon" width="{0}" height="{0}" role="img" aria-label="{2}"><use href="#jv-{1}"></use></svg>',
            size,
            name,
            label,
        )

    return format_html(
        '<svg class="icon" width="{0}" height="{0}" aria-hidden="true"><use href="#jv-{1}"></use></svg>',
        size,
        name,
    )


@register.filter
def compact(value):
    """1,284 / 12.9K / 4.2M."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return value

    for threshold, divisor, suffix in ((1_000_000, 1_000_000, "M"), (10_000, 1_000, "K")):
        if abs(number) >= threshold:
            return f"{number / divisor:.1f}".rstrip("0").rstrip(".") + suffix

    return f"{int(number):,}" if number.is_integer() else f"{number:,}"


@register.simple_tag
def badge(text, tone="neutral"):
    return format_html('<span class="badge badge--{}">{}</span>', tone, text)


@register.simple_tag
def status_badge(status):
    value = getattr(status, "status", status)

    return format_html(
        '<span class="badge badge--{} badge--dot">{}</span>',
        STATUS_TONES.get(value, "neutral"),
        Ticket.Status(value).label,
    )


@register.simple_tag
def priority_badge(priority):
    value = getattr(priority, "priority", priority)

    return format_html(
        '<span class="badge badge--{}">{}</span>',
        PRIORITY_TONES.get(value, "neutral"),
        Ticket.Priority(value).label,
    )


@register.simple_tag
def due_badge(ticket, now=None):
    """"Resolution overdue" / "due in 5 h" for an active ticket."""

    if not ticket.is_active:
        return ""

    now = now or timezone.now()
    kind = ticket.overdue_kind(now)

    if kind:
        return format_html('<span class="badge badge--critical">{} overdue</span>', "Response" if kind == "response" else "Resolution")

    if ticket.resolve_due_at is None:
        return ""

    hours = (ticket.resolve_due_at - now).total_seconds() / 3600

    if hours < 24:
        text = f"due in {int(hours)} h" if hours >= 1 else "due within the hour"
        tone = "warning"
    else:
        text = f"due in {int(hours // 24)} d"
        tone = "neutral"

    return format_html('<span class="badge badge--{}">{}</span>', tone, text)


@register.filter
def event_icon(event):
    return EVENT_ICONS.get(event.type, "activity")


@register.filter
def event_tone(event):
    return EVENT_TONES.get(event.type, "neutral")


@register.filter
def person(user):
    if user is None:
        return "Deleted user"

    return user.get_full_name() or user.email


@register.filter
def initials_of(user):
    if user is None:
        return "?"

    words = user.get_full_name().split() or [user.email]

    return "".join(word[0] for word in words[:2]).upper()


@register.filter
def human_size(size):
    return _human_size(size)


@register.simple_tag(takes_context=True)
def query(context, **changes):
    """The current query string with some parameters changed (None drops one)."""

    params = context["request"].GET.copy()

    for key, value in changes.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value

    encoded = params.urlencode()

    return f"?{encoded}" if encoded else "?"
