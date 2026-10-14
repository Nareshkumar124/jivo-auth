"""
Notifications for ticket activity: an in-app notification per recipient,
plus an email with a link to the ticket. Sent after the surrounding
transaction commits, so a rolled-back change never notifies anyone.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.urls import reverse

from .models import Notification


logger = logging.getLogger(__name__)


def ticket_url(ticket):
    return f"{settings.PUBLIC_URL}{reverse('support:ticket-detail', args=[ticket.pk])}"


def display_name(user):
    if user is None:
        return "Someone"

    return user.get_full_name() or user.email


def _send_email(people, subject, body):
    """One message, every recipient in Bcc (they don't see each other)."""

    try:
        EmailMessage(
            subject=subject,
            body=body,
            bcc=[user.email for user in people],
        ).send()
    except Exception:
        # A mail outage must not fail the ticket action.
        logger.exception("Failed to send support email %r to %d recipient(s)", subject, len(people))


def notify(recipients, *, ticket, kind, title, body, exclude=None):
    """
    Notify each distinct active recipient (minus `exclude`, usually the
    actor) about `ticket`. Returns the number of people notified.
    """

    seen = set()
    people = []

    for user in recipients:
        if user is None or not user.is_active or user.pk in seen:
            continue

        if exclude is not None and user.pk == exclude.pk:
            continue

        seen.add(user.pk)
        people.append(user)

    if not people:
        return 0

    Notification.objects.bulk_create(
        [
            Notification(user=user, ticket=ticket, kind=kind, title=title, body=body)
            for user in people
        ]
    )

    subject = f"[{ticket.reference}] {title}"
    message = f"{body}\n\nOpen the ticket: {ticket_url(ticket)}\n"

    transaction.on_commit(lambda: _send_email(people, subject, message))

    return len(people)


def team_members(ticket):
    if ticket.team_id is None:
        return []

    return list(ticket.team.members.filter(is_active=True))


def unread_count(user):
    if not getattr(user, "is_authenticated", False):
        return 0

    return Notification.objects.filter(user=user, read_at__isnull=True).count()
