from django import template
from django.utils.html import format_html


register = template.Library()


@register.simple_tag
def icon(name, size=18, label=""):
    """An icon from the sprite in admin/base_site.html."""

    if label:
        return format_html(
            '<svg class="jv-icon" width="{0}" height="{0}" role="img" '
            'aria-label="{2}"><use href="#jv-{1}"></use></svg>',
            size,
            name,
            label,
        )

    return format_html(
        '<svg class="jv-icon" width="{0}" height="{0}" aria-hidden="true">'
        '<use href="#jv-{1}"></use></svg>',
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

    # Below 10,000 the full number still reads at a glance.
    for threshold, divisor, suffix in (
        (1_000_000_000, 1_000_000_000, "B"),
        (1_000_000, 1_000_000, "M"),
        (10_000, 1_000, "K"),
    ):
        if abs(number) >= threshold:
            text = f"{number / divisor:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"

    return f"{int(number):,}" if number.is_integer() else f"{number:,}"


@register.filter
def initials(user):
    name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".split()

    if len(name) >= 2:
        return (name[0][0] + name[-1][0]).upper()

    source = name[0] if name else (getattr(user, "email", "") or "?")

    return source[:2].upper()


@register.filter
def password_status(encoded):
    """Label for a password hash: set (and how it's hashed) or not."""

    from django.contrib.auth.hashers import UNUSABLE_PASSWORD_PREFIX, identify_hasher

    if not encoded or encoded.startswith(UNUSABLE_PASSWORD_PREFIX):
        return {"label": "No password", "tone": "neutral", "algorithm": ""}

    try:
        algorithm = identify_hasher(encoded).algorithm
    except ValueError:
        return {"label": "Unknown hash", "tone": "warning", "algorithm": ""}

    return {"label": "Password set", "tone": "good", "algorithm": f"Stored as {algorithm}"}
