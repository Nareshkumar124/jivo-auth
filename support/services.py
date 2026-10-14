"""
Everything that changes a ticket. Each function runs in a transaction,
locks the ticket row (so two people can't claim it at once), records the
TicketEvent and queues the notifications.

Views check permissions with support.access before calling these; the
functions re-check the state after taking the lock, because it may have
changed since the page was rendered, and raise SupportError with a message
for the user.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import access
from .attachments import clean_filename, validate_upload
from .models import (
    SupportSettings,
    Ticket,
    TicketAttachment,
    TicketComment,
    TicketEvent,
)
from .notifications import display_name, notify, team_members


class SupportError(Exception):
    """A ticket action that can't be done now; the message is for the user."""


Status = Ticket.Status

# From each status, where it may move next (resolve, close and reopen have
# their own functions; the rest goes through change_status).
TRANSITIONS = {
    Status.OPEN: {Status.IN_PROGRESS, Status.WAITING, Status.RESOLVED, Status.CLOSED},
    Status.REOPENED: {Status.IN_PROGRESS, Status.WAITING, Status.RESOLVED, Status.CLOSED},
    Status.IN_PROGRESS: {Status.OPEN, Status.WAITING, Status.RESOLVED, Status.CLOSED},
    Status.WAITING: {Status.OPEN, Status.IN_PROGRESS, Status.RESOLVED, Status.CLOSED},
    Status.RESOLVED: {Status.CLOSED, Status.REOPENED},
    Status.CLOSED: {Status.REOPENED},
}

# Statuses change_status() may set directly.
SIMPLE_STATUSES = (Status.OPEN, Status.IN_PROGRESS, Status.WAITING)


def status_label(status):
    return Status(status).label if status else ""


def lock(ticket):
    # Only the ticket row is locked: PostgreSQL refuses FOR UPDATE on the
    # nullable side of the joins with_related() adds.
    return Ticket.objects.select_for_update(of=("self",)).with_related().get(pk=ticket.pk)


def record_event(ticket, type, actor=None, **details):
    return TicketEvent.objects.create(
        ticket=ticket,
        actor=actor,
        type=type,
        details={key: value for key, value in details.items() if value not in (None, "")},
    )


def save(ticket, *fields):
    ticket.save(update_fields=[*fields, "updated_at"])


def route(application, department):
    """The team a new ticket for the application goes to, or None."""

    team = application.support_team

    if team is not None and team.is_active:
        return team

    team = department.default_team

    if team is not None and team.is_active:
        return team

    return None


def sla_start(ticket):
    return ticket.reopened_at or ticket.created_at


def apply_sla(ticket, config, start=None):
    ticket.response_due_at, ticket.resolve_due_at = config.due_dates(
        ticket.priority,
        start or sla_start(ticket),
    )


# ---- Creation --------------------------------------------------------------

@transaction.atomic
def create_ticket(*, created_by, department, application, subject, description, files=(), config=None):
    config = config or SupportSettings.load()
    team = route(application, department)
    now = timezone.now()

    ticket = Ticket(
        subject=subject.strip(),
        description=description.strip(),
        department=department,
        application=application,
        team=team,
        created_by=created_by,
        created_at=now,
    )
    apply_sla(ticket, config, now)
    ticket.save()

    record_event(
        ticket,
        TicketEvent.Type.CREATED,
        actor=created_by,
        department=department.name,
        application=application.name,
        team=team.name if team else "",
    )

    for uploaded in files:
        add_attachment(ticket, uploaded, uploaded_by=created_by, config=config, record=False)

    if team is not None and config.notify_team_on_new_ticket:
        notify(
            team_members(ticket),
            ticket=ticket,
            kind="new_ticket",
            title=f"New ticket for {team.name}: {ticket.subject}",
            body=(
                f"{display_name(created_by)} reported a problem with "
                f"{application.name} ({department.name}).\n\n{ticket.description}"
            ),
            exclude=created_by,
        )

    return ticket


def add_attachment(ticket, uploaded, *, uploaded_by, comment=None, internal=False, config=None, record=True):
    config = config or SupportSettings.load()
    content_type = validate_upload(uploaded, config)

    attachment = TicketAttachment(
        ticket=ticket,
        comment=comment,
        original_name=clean_filename(uploaded.name),
        content_type=content_type,
        size=uploaded.size,
        uploaded_by=uploaded_by,
        is_internal=internal,
    )
    attachment.file.save(attachment.original_name, uploaded, save=False)
    attachment.save()

    if record:
        record_event(ticket, TicketEvent.Type.ATTACHMENT_ADDED, actor=uploaded_by, name=attachment.original_name)

    return attachment


def validate_files(files, config=None):
    """Raise ValidationError with every problem before anything is saved."""

    config = config or SupportSettings.load()
    errors = []

    for uploaded in files:
        try:
            validate_upload(uploaded, config)
        except ValidationError as error:
            errors.extend(error.messages)

    if errors:
        raise ValidationError(errors)


# ---- Assignment ------------------------------------------------------------

def _set_assignee(ticket, assignee, actor, note=""):
    """Assign or unassign (assignee None), keeping the status in step."""

    previous = ticket.assignee
    old_status = ticket.status
    now = timezone.now()

    ticket.assignee = assignee
    ticket.assigned_at = now if assignee else None
    fields = ["assignee", "assigned_at"]

    if assignee is not None and ticket.status in Ticket.OPEN_STATUSES:
        ticket.status = Status.IN_PROGRESS
        fields.append("status")
    elif assignee is None and ticket.status == Status.IN_PROGRESS:
        ticket.status = Status.OPEN
        fields.append("status")

    save(ticket, *fields)

    if assignee is None:
        record_event(ticket, TicketEvent.Type.UNASSIGNED, actor=actor, **{"from": display_name(previous)}, note=note)
    else:
        record_event(
            ticket,
            TicketEvent.Type.ASSIGNED,
            actor=actor,
            **{"from": display_name(previous) if previous else "", "to": display_name(assignee)},
            note=note,
        )

    if ticket.status != old_status:
        record_event(
            ticket,
            TicketEvent.Type.STATUS_CHANGED,
            actor=actor,
            **{"from": status_label(old_status), "to": status_label(ticket.status)},
        )

    if assignee is not None and (actor is None or assignee.pk != actor.pk):
        notify(
            [assignee],
            ticket=ticket,
            kind="assigned",
            title=f"Ticket assigned to you: {ticket.subject}",
            body=f"{display_name(actor)} assigned you {ticket.reference} ({ticket.application.name}).",
        )

    if previous is not None and assignee is not None and previous.pk != assignee.pk:
        notify(
            [previous],
            ticket=ticket,
            kind="reassigned",
            title=f"Ticket reassigned: {ticket.subject}",
            body=f"{display_name(actor)} reassigned {ticket.reference} to {display_name(assignee)}.",
            exclude=actor,
        )

    if ticket.status != old_status:
        notify(
            [ticket.created_by],
            ticket=ticket,
            kind="status",
            title=f"Your ticket is now {status_label(ticket.status).lower()}: {ticket.subject}",
            body=(
                f"{display_name(assignee or actor)} is working on {ticket.reference}."
                if ticket.status == Status.IN_PROGRESS
                else f"{ticket.reference} is back in the queue."
            ),
            exclude=actor,
        )


@transaction.atomic
def claim(ticket, user):
    ticket = lock(ticket)

    if not ticket.is_active:
        raise SupportError("This ticket is no longer active.")

    if ticket.assignee_id is not None:
        if ticket.assignee_id == user.pk:
            raise SupportError("This ticket is already yours.")

        raise SupportError(f"{display_name(ticket.assignee)} claimed this ticket first.")

    if not access.can_claim(user, ticket):
        raise SupportError("You can't claim tickets of this team.")

    _set_assignee(ticket, user, actor=user)

    return ticket


@transaction.atomic
def assign(ticket, actor, assignee, note=""):
    """Assign to a member of the ticket's team, or unassign with None."""

    ticket = lock(ticket)

    if not ticket.is_active:
        raise SupportError("This ticket is no longer active.")

    if assignee is not None:
        if not assignee.is_active:
            raise SupportError(f"{display_name(assignee)} is deactivated.")

        if not access.assignable_users(ticket).filter(pk=assignee.pk).exists():
            raise SupportError(f"{display_name(assignee)} isn't a member of {ticket.team}.")

    if (assignee.pk if assignee else None) == ticket.assignee_id:
        raise SupportError(
            f"The ticket is already assigned to {display_name(assignee)}."
            if assignee
            else "The ticket isn't assigned to anyone."
        )

    _set_assignee(ticket, assignee, actor=actor, note=note)

    return ticket


# ---- Status ----------------------------------------------------------------

def _check_transition(ticket, status):
    if status not in TRANSITIONS.get(ticket.status, set()):
        raise SupportError(
            f"A ticket that is {status_label(ticket.status).lower()} can't be "
            f"set to {status_label(status).lower()}."
        )


@transaction.atomic
def change_status(ticket, actor, status, note=""):
    """Open, in progress or waiting. Resolve, close and reopen have their own
    functions because they need more than a status."""

    ticket = lock(ticket)

    if status not in SIMPLE_STATUSES:
        raise SupportError("Use the resolve, close or reopen actions for that status.")

    if status == ticket.status:
        raise SupportError(f"The ticket is already {status_label(status).lower()}.")

    _check_transition(ticket, status)

    old_status = ticket.status
    ticket.status = status
    fields = ["status"]

    # Taking a ticket into progress without an owner makes the actor its owner.
    if status == Status.IN_PROGRESS and ticket.assignee_id is None and access.can_handle(actor, ticket):
        ticket.assignee = actor
        ticket.assigned_at = timezone.now()
        fields += ["assignee", "assigned_at"]

    save(ticket, *fields)

    record_event(
        ticket,
        TicketEvent.Type.STATUS_CHANGED,
        actor=actor,
        **{"from": status_label(old_status), "to": status_label(status)},
        note=note,
    )

    if "assignee" in fields:
        record_event(ticket, TicketEvent.Type.ASSIGNED, actor=actor, to=display_name(actor))

    notify(
        [ticket.created_by, ticket.assignee],
        ticket=ticket,
        kind="status",
        title=f"Ticket {status_label(status).lower()}: {ticket.subject}",
        body=f"{display_name(actor)} set {ticket.reference} to {status_label(status).lower()}."
        + (f"\n\n{note}" if note else ""),
        exclude=actor,
    )

    return ticket


@transaction.atomic
def request_information(ticket, actor, message, files=(), config=None):
    """Ask the user for more details: a reply plus the waiting status."""

    ticket = lock(ticket)

    if not ticket.is_active:
        raise SupportError("This ticket is no longer active.")

    old_status = ticket.status
    comment = TicketComment.objects.create(ticket=ticket, author=actor, body=message.strip(), is_info_request=True)

    for uploaded in files:
        add_attachment(ticket, uploaded, uploaded_by=actor, comment=comment, config=config, record=False)

    fields = []

    if ticket.first_response_at is None:
        ticket.first_response_at = timezone.now()
        fields.append("first_response_at")

    if ticket.status != Status.WAITING:
        ticket.status = Status.WAITING
        fields.append("status")

    save(ticket, *fields)

    record_event(ticket, TicketEvent.Type.INFO_REQUESTED, actor=actor, note=message.strip()[:200])

    if old_status != ticket.status:
        record_event(
            ticket,
            TicketEvent.Type.STATUS_CHANGED,
            actor=actor,
            **{"from": status_label(old_status), "to": status_label(ticket.status)},
        )

    notify(
        [ticket.created_by],
        ticket=ticket,
        kind="info_requested",
        title=f"More information needed: {ticket.subject}",
        body=f"{display_name(actor)} asked:\n\n{message.strip()}",
        exclude=actor,
    )

    return comment


@transaction.atomic
def add_comment(ticket, author, body, *, internal=False, files=(), config=None):
    ticket = lock(ticket)

    if internal and not access.can_view_internal(author, ticket):
        raise SupportError("Only the responsible team can add internal notes.")

    if ticket.status == Status.CLOSED and not access.can_handle(author, ticket):
        raise SupportError("This ticket is closed. Reopen it to continue the conversation.")

    comment = TicketComment.objects.create(ticket=ticket, author=author, body=body.strip(), is_internal=internal)

    for uploaded in files:
        add_attachment(ticket, uploaded, uploaded_by=author, comment=comment, internal=internal, config=config, record=False)

    fields = []
    old_status = ticket.status
    from_team = access.can_handle(author, ticket) and not access.is_creator(author, ticket)

    if internal:
        record_event(ticket, TicketEvent.Type.NOTE_ADDED, actor=author)
        save(ticket)

        notify(
            [ticket.assignee],
            ticket=ticket,
            kind="note",
            title=f"Internal note on {ticket.reference}: {ticket.subject}",
            body=f"{display_name(author)} wrote an internal note:\n\n{comment.body}",
            exclude=author,
        )

        return comment

    record_event(ticket, TicketEvent.Type.COMMENTED, actor=author)

    if from_team:
        if ticket.first_response_at is None:
            ticket.first_response_at = timezone.now()
            fields.append("first_response_at")

        save(ticket, *fields)

        notify(
            [ticket.created_by],
            ticket=ticket,
            kind="reply",
            title=f"New reply on your ticket: {ticket.subject}",
            body=f"{display_name(author)} replied:\n\n{comment.body}",
            exclude=author,
        )

        return comment

    # The user answered: a ticket waiting for them is active again.
    if ticket.status == Status.WAITING:
        ticket.status = Status.IN_PROGRESS if ticket.assignee_id else Status.OPEN
        fields.append("status")

    save(ticket, *fields)

    if ticket.status != old_status:
        record_event(
            ticket,
            TicketEvent.Type.STATUS_CHANGED,
            actor=author,
            **{"from": status_label(old_status), "to": status_label(ticket.status)},
            note="The user replied",
        )

    notify(
        [ticket.assignee] if ticket.assignee_id else team_members(ticket),
        ticket=ticket,
        kind="reply",
        title=f"New reply on {ticket.reference}: {ticket.subject}",
        body=f"{display_name(author)} replied:\n\n{comment.body}",
        exclude=author,
    )

    return comment


@transaction.atomic
def resolve(ticket, actor, resolution):
    ticket = lock(ticket)

    if not ticket.is_active:
        raise SupportError("This ticket is no longer active.")

    _check_transition(ticket, Status.RESOLVED)

    old_status = ticket.status
    now = timezone.now()

    ticket.status = Status.RESOLVED
    ticket.resolved_at = now
    ticket.resolved_by = actor
    ticket.resolution = resolution.strip()
    fields = ["status", "resolved_at", "resolved_by", "resolution"]

    if ticket.first_response_at is None:
        ticket.first_response_at = now
        fields.append("first_response_at")

    if ticket.assignee_id is None and access.can_handle(actor, ticket):
        ticket.assignee = actor
        ticket.assigned_at = now
        fields += ["assignee", "assigned_at"]

    save(ticket, *fields)

    record_event(ticket, TicketEvent.Type.RESOLVED, actor=actor, **{"from": status_label(old_status)}, note=ticket.resolution[:200])

    notify(
        [ticket.created_by, ticket.assignee],
        ticket=ticket,
        kind="resolved",
        title=f"Ticket resolved: {ticket.subject}",
        body=(
            f"{display_name(actor)} resolved {ticket.reference}.\n\nResolution:\n{ticket.resolution}"
            "\n\nIf the problem persists, reopen the ticket from its page."
        ),
        exclude=actor,
    )

    return ticket


@transaction.atomic
def close(ticket, actor, note=""):
    ticket = lock(ticket)

    if ticket.status == Status.CLOSED:
        raise SupportError("This ticket is already closed.")

    _check_transition(ticket, Status.CLOSED)

    old_status = ticket.status
    ticket.status = Status.CLOSED
    ticket.closed_at = timezone.now()

    save(ticket, "status", "closed_at")

    record_event(ticket, TicketEvent.Type.CLOSED, actor=actor, **{"from": status_label(old_status)}, note=note.strip()[:200])

    notify(
        [ticket.created_by, ticket.assignee],
        ticket=ticket,
        kind="closed",
        title=f"Ticket closed: {ticket.subject}",
        body=f"{display_name(actor)} closed {ticket.reference}." + (f"\n\n{note.strip()}" if note.strip() else ""),
        exclude=actor,
    )

    return ticket


@transaction.atomic
def reopen(ticket, actor, reason, config=None):
    ticket = lock(ticket)
    config = config or SupportSettings.load()

    if not ticket.is_done:
        raise SupportError("Only resolved or closed tickets can be reopened.")

    if not access.can_reopen(actor, ticket, config):
        raise SupportError("This ticket can't be reopened any more. Create a new ticket instead.")

    old_status = ticket.status
    now = timezone.now()

    ticket.status = Status.REOPENED
    ticket.reopened_at = now
    ticket.reopen_count += 1
    ticket.resolved_at = None
    ticket.closed_at = None
    ticket.first_response_at = None
    apply_sla(ticket, config, now)

    save(
        ticket,
        "status", "reopened_at", "reopen_count", "resolved_at", "closed_at",
        "first_response_at", "response_due_at", "resolve_due_at",
    )

    record_event(ticket, TicketEvent.Type.REOPENED, actor=actor, **{"from": status_label(old_status)}, reason=reason.strip()[:200])

    if reason.strip():
        TicketComment.objects.create(ticket=ticket, author=actor, body=reason.strip())

    notify(
        [ticket.created_by, ticket.assignee] if ticket.assignee_id else [ticket.created_by, *team_members(ticket)],
        ticket=ticket,
        kind="reopened",
        title=f"Ticket reopened: {ticket.subject}",
        body=f"{display_name(actor)} reopened {ticket.reference}." + (f"\n\n{reason.strip()}" if reason.strip() else ""),
        exclude=actor,
    )

    return ticket


# ---- Priority and team -----------------------------------------------------

@transaction.atomic
def change_priority(ticket, actor, priority, config=None):
    ticket = lock(ticket)
    config = config or SupportSettings.load()

    if not ticket.is_active:
        raise SupportError("This ticket is no longer active.")

    if priority == ticket.priority:
        raise SupportError(f"The priority is already {Ticket.Priority(priority).label.lower()}.")

    old = ticket.priority
    ticket.priority = priority
    apply_sla(ticket, config)

    save(ticket, "priority", "response_due_at", "resolve_due_at")

    record_event(
        ticket,
        TicketEvent.Type.PRIORITY_CHANGED,
        actor=actor,
        **{"from": Ticket.Priority(old).label, "to": Ticket.Priority(priority).label},
    )

    notify(
        [ticket.assignee],
        ticket=ticket,
        kind="priority",
        title=f"Priority is now {Ticket.Priority(priority).label.lower()}: {ticket.subject}",
        body=f"{display_name(actor)} changed the priority of {ticket.reference} from {Ticket.Priority(old).label.lower()} to {Ticket.Priority(priority).label.lower()}.",
        exclude=actor,
    )

    return ticket


@transaction.atomic
def change_team(ticket, actor, team, note=""):
    ticket = lock(ticket)

    if not ticket.is_active:
        raise SupportError("This ticket is no longer active.")

    if team is not None and not team.is_active:
        raise SupportError(f"{team} is inactive.")

    if (team.pk if team else None) == ticket.team_id:
        raise SupportError("The ticket is already with that team.")

    old_team = ticket.team
    ticket.team = team
    save(ticket, "team")

    record_event(
        ticket,
        TicketEvent.Type.TEAM_CHANGED,
        actor=actor,
        **{"from": old_team.name if old_team else "", "to": team.name if team else ""},
        note=note,
    )

    # The assignee stays only if they belong to the new team.
    if ticket.assignee_id and not access.assignable_users(ticket).filter(pk=ticket.assignee_id).exists():
        _set_assignee(ticket, None, actor=actor, note="moved to another team")

    if team is not None:
        notify(
            team_members(ticket),
            ticket=ticket,
            kind="team",
            title=f"Ticket routed to {team.name}: {ticket.subject}",
            body=f"{display_name(actor)} moved {ticket.reference} to {team.name}.",
            exclude=actor,
        )

    return ticket


# ---- Automatic closing -----------------------------------------------------

def close_stale_resolved(now=None, config=None):
    """
    Close resolved tickets the user hasn't answered within the configured
    number of days. Returns how many were closed.
    """

    from datetime import timedelta

    config = config or SupportSettings.load()

    if not config.auto_close_after_days:
        return 0

    now = now or timezone.now()
    cutoff = now - timedelta(days=config.auto_close_after_days)
    closed = 0

    stale = (
        Ticket.objects
        .filter(status=Status.RESOLVED, resolved_at__lt=cutoff)
        # A user who answered after the resolution keeps the ticket open.
        .exclude(comments__author=F("created_by"), comments__created_at__gt=F("resolved_at"))
        .with_related()
    )

    for ticket in stale:
        try:
            close(ticket, None, note=f"Closed automatically after {config.auto_close_after_days} days without a reply.")
        except SupportError:
            continue

        closed += 1

    return closed


# ---- Teams and roles ---------------------------------------------------------

@transaction.atomic
def reassign_member_tickets(team, actor, member, to=None, note=""):
    """
    Move every active ticket of `team` assigned to `member` to `to` (another
    member of the team) or back to the queue (None). Returns (moved, skipped
    messages).
    """

    tickets = Ticket.objects.filter(team=team, assignee=member).active().with_related()
    moved = 0
    skipped = []

    for ticket in tickets:
        try:
            assign(ticket, actor, to, note=note or f"reassigned from {display_name(member)}")
        except SupportError as error:
            skipped.append(f"{ticket.reference}: {error}")
            continue

        moved += 1

    return moved, skipped


@transaction.atomic
def add_member(team, user, role, actor=None):
    from .models import TeamMembership

    if not user.is_active:
        raise SupportError(f"{display_name(user)} is deactivated.")

    membership, created = TeamMembership.objects.get_or_create(team=team, user=user, defaults={"role": role})

    if not created:
        raise SupportError(f"{display_name(user)} is already in {team.name}.")

    access.forget(user)

    return membership


@transaction.atomic
def set_member_role(membership, role):
    membership.role = role
    membership.save(update_fields=["role"])
    access.forget(membership.user)

    return membership


@transaction.atomic
def remove_member(membership, actor):
    """Take someone out of a team. Their active tickets of that team go back
    to the queue, so nothing stays with a person who can't see it."""

    team, user = membership.team, membership.user
    returned, _ = reassign_member_tickets(team, actor, user, None, note=f"{display_name(user)} left {team.name}")
    membership.delete()
    access.forget(user)

    return returned


@transaction.atomic
def grant_administrator(user, actor):
    from .models import SupportAdministrator

    if not user.is_active:
        raise SupportError(f"{display_name(user)} is deactivated.")

    if user.is_superuser:
        raise SupportError(f"{display_name(user)} is a superuser and always an administrator.")

    _, created = SupportAdministrator.objects.get_or_create(user=user, defaults={"added_by": actor})

    if not created:
        raise SupportError(f"{display_name(user)} is already an administrator.")

    access.forget(user)


@transaction.atomic
def revoke_administrator(user, actor):
    from .models import SupportAdministrator

    if user.pk == actor.pk:
        raise SupportError("You can't remove your own administrator role. Ask another administrator.")

    if user.is_superuser:
        raise SupportError(f"{display_name(user)} is a superuser and always an administrator.")

    deleted, _ = SupportAdministrator.objects.filter(user=user).delete()

    if not deleted:
        raise SupportError(f"{display_name(user)} isn't an administrator.")

    access.forget(user)
