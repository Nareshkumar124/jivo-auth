"""
Support tickets.

Routing follows Department -> Application -> Team: an application belongs
to a department and names the team responsible for it (both on
applications.Application). A new ticket for the application goes to that
team; existing tickets keep their team when the mapping changes.

Every change to a ticket goes through support.services, which records a
TicketEvent and sends notifications. The models themselves hold no
workflow logic.
"""

import os
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


TICKET_PREFIX = "JT"


class Department(models.Model):

    name = models.CharField(
        max_length=100,
        unique=True,
    )

    description = models.TextField(
        blank=True,
    )

    default_team = models.ForeignKey(
        "Team",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="default_for_departments",
        help_text=(
            "Receives tickets for applications in this department that "
            "have no responsible team of their own."
        ),
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Inactive departments can't be chosen on new tickets.",
    )

    created_at = models.DateTimeField(
        default=timezone.now,
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Team(models.Model):

    name = models.CharField(
        max_length=100,
        unique=True,
    )

    description = models.TextField(
        blank=True,
    )

    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="TeamMembership",
        related_name="support_teams",
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Inactive teams receive no new tickets and their members lose "
            "the team workspace."
        ),
    )

    created_at = models.DateTimeField(
        default=timezone.now,
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def active_member_ids(self):
        return list(
            self.memberships
            .filter(user__is_active=True)
            .values_list("user_id", flat=True)
        )


class TeamMembership(models.Model):

    class Role(models.TextChoices):
        MEMBER = "member", "Member"
        MANAGER = "manager", "Team manager"

    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="memberships",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="team_memberships",
    )

    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.MEMBER,
        help_text=(
            "Team managers may always assign tickets, change priorities and "
            "reassign a member's tickets, and see the team's statistics."
        ),
    )

    joined_at = models.DateTimeField(
        default=timezone.now,
    )

    class Meta:
        ordering = ["team", "user__email"]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "user"],
                name="support_membership_unique",
                violation_error_message="This person is already in the team.",
            ),
        ]
        verbose_name = "team member"
        verbose_name_plural = "team members"

    def __str__(self):
        return f"{self.user} in {self.team}"


class SupportAdministrator(models.Model):
    """
    A ticket-system administrator: manages every ticket, department, team,
    application routing, role, report and setting at /manage/. Granted in
    the ticket system itself (People and roles). Superusers always are one.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="support_administrator",
    )

    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    created_at = models.DateTimeField(
        default=timezone.now,
    )

    class Meta:
        ordering = ["user__email"]
        verbose_name = "ticket administrator"
        verbose_name_plural = "ticket administrators"

    def __str__(self):
        return str(self.user)


class SupportSettings(models.Model):
    """
    One row (pk=1) of workflow settings, edited in the admin. Load it with
    SupportSettings.load().
    """

    PRIORITY_FIELDS = ("urgent", "high", "medium", "low")

    response_hours_urgent = models.PositiveIntegerField(default=1, verbose_name="Urgent: first response within (hours)")
    response_hours_high = models.PositiveIntegerField(default=4, verbose_name="High: first response within (hours)")
    response_hours_medium = models.PositiveIntegerField(default=8, verbose_name="Medium: first response within (hours)")
    response_hours_low = models.PositiveIntegerField(default=24, verbose_name="Low: first response within (hours)")

    resolution_hours_urgent = models.PositiveIntegerField(default=8, verbose_name="Urgent: resolve within (hours)")
    resolution_hours_high = models.PositiveIntegerField(default=24, verbose_name="High: resolve within (hours)")
    resolution_hours_medium = models.PositiveIntegerField(default=72, verbose_name="Medium: resolve within (hours)")
    resolution_hours_low = models.PositiveIntegerField(default=168, verbose_name="Low: resolve within (hours)")

    members_can_assign = models.BooleanField(
        default=True,
        verbose_name="Team members may assign tickets to teammates",
        help_text="Otherwise only team managers and administrators assign; members can still claim unassigned tickets.",
    )

    members_can_change_priority = models.BooleanField(
        default=True,
        verbose_name="Team members may change priorities",
        help_text="Otherwise only team managers and administrators.",
    )

    users_can_reopen = models.BooleanField(
        default=True,
        verbose_name="Users may reopen their own tickets",
    )

    reopen_window_days = models.PositiveIntegerField(
        default=14,
        verbose_name="Reopen window (days)",
        help_text="How long after resolution a user may reopen their ticket. 0 means any time.",
    )

    auto_close_after_days = models.PositiveIntegerField(
        default=7,
        verbose_name="Close resolved tickets after (days)",
        help_text=(
            "Resolved tickets with no reply from the user are closed by "
            "`manage.py close_resolved_tickets` after this many days. 0 disables it."
        ),
    )

    max_attachment_mb = models.PositiveIntegerField(
        default=10,
        verbose_name="Largest attachment (MB)",
    )

    allowed_extensions = models.CharField(
        max_length=500,
        default="png, jpg, jpeg, gif, webp, pdf, txt, log, csv, xlsx, xls, docx, doc, pptx, zip, json",
        help_text="Comma-separated file extensions users may attach.",
    )

    notify_team_on_new_ticket = models.BooleanField(
        default=True,
        verbose_name="Email the team about new tickets",
        help_text="Every active member of the responsible team gets an email when a ticket arrives.",
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        verbose_name = "support settings"
        verbose_name_plural = "support settings"

    def __str__(self):
        return "Support settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def due_hours(self, priority):
        """(first response, resolution) deadlines in hours; 0 means none."""

        return (
            getattr(self, f"response_hours_{priority}", 0),
            getattr(self, f"resolution_hours_{priority}", 0),
        )

    def due_dates(self, priority, start):
        response, resolution = self.due_hours(priority)

        return (
            start + timedelta(hours=response) if response else None,
            start + timedelta(hours=resolution) if resolution else None,
        )

    def allowed_extension_set(self):
        return {
            item.strip().lower().lstrip(".")
            for item in self.allowed_extensions.split(",")
            if item.strip()
        }

    @property
    def max_attachment_bytes(self):
        return self.max_attachment_mb * 1024 * 1024


class TicketQuerySet(models.QuerySet):

    def active(self):
        return self.filter(status__in=Ticket.ACTIVE_STATUSES)

    def unassigned(self):
        return self.active().filter(assignee__isnull=True)

    def overdue(self, now=None):
        now = now or timezone.now()

        return self.active().filter(
            Q(resolve_due_at__lt=now)
            | Q(first_response_at__isnull=True, response_due_at__lt=now)
        )

    def with_related(self):
        return self.select_related(
            "department",
            "application",
            "team",
            "assignee",
            "created_by",
        )


class Ticket(models.Model):

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        WAITING = "waiting", "Waiting for information"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"
        REOPENED = "reopened", "Reopened"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    # Open and reopened tickets wait for someone to pick them up.
    OPEN_STATUSES = (Status.OPEN, Status.REOPENED)
    ACTIVE_STATUSES = (Status.OPEN, Status.REOPENED, Status.IN_PROGRESS, Status.WAITING)
    DONE_STATUSES = (Status.RESOLVED, Status.CLOSED)

    PRIORITY_ORDER = {
        Priority.URGENT: 0,
        Priority.HIGH: 1,
        Priority.MEDIUM: 2,
        Priority.LOW: 3,
    }

    subject = models.CharField(
        max_length=200,
    )

    description = models.TextField()

    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="tickets",
    )

    application = models.ForeignKey(
        "applications.Application",
        on_delete=models.PROTECT,
        related_name="tickets",
    )

    # The team responsible when the ticket was created (or reassigned by an
    # administrator); unchanged when the application's mapping changes.
    team = models.ForeignKey(
        Team,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tickets",
    )

    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_tickets",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="created_tickets",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )

    priority = models.CharField(
        max_length=10,
        choices=Priority.choices,
        default=Priority.MEDIUM,
        db_index=True,
    )

    created_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    first_response_at = models.DateTimeField(null=True, blank=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    reopened_at = models.DateTimeField(null=True, blank=True)

    # SLA deadlines, set from SupportSettings when the ticket is created and
    # recomputed when its priority changes.
    response_due_at = models.DateTimeField(null=True, blank=True)
    resolve_due_at = models.DateTimeField(null=True, blank=True)

    resolution = models.TextField(
        blank=True,
        help_text="What was done to resolve the ticket.",
    )

    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    reopen_count = models.PositiveIntegerField(
        default=0,
    )

    objects = TicketQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["team", "status"]),
            models.Index(fields=["assignee", "status"]),
            models.Index(fields=["created_by", "created_at"]),
            models.Index(fields=["application", "status"]),
            models.Index(fields=["department", "status"]),
            models.Index(fields=["resolve_due_at"]),
        ]

    def __str__(self):
        return f"{self.reference} {self.subject}"

    @property
    def reference(self):
        return f"{TICKET_PREFIX}-{self.pk:04d}" if self.pk else f"{TICKET_PREFIX}-new"

    @staticmethod
    def parse_reference(text):
        """The ticket number in "JT-0042", "jt42", "#42" or "42", else None."""

        value = (text or "").strip().upper().lstrip("#")

        if value.startswith(TICKET_PREFIX):
            value = value[len(TICKET_PREFIX):].lstrip("-")

        return int(value) if value.isdigit() else None

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    @property
    def is_done(self):
        return self.status in self.DONE_STATUSES

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    def overdue_kind(self, now=None):
        """"resolution", "response" or "" (not overdue)."""

        if not self.is_active:
            return ""

        now = now or timezone.now()

        if self.resolve_due_at and self.resolve_due_at < now:
            return "resolution"

        if self.first_response_at is None and self.response_due_at and self.response_due_at < now:
            return "response"

        return ""

    @property
    def is_overdue(self):
        return bool(self.overdue_kind())

    @property
    def done_at(self):
        return self.closed_at or self.resolved_at

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("support:ticket-detail", args=[self.pk])

    @property
    def status_tone(self):
        return STATUS_TONES.get(self.status, "neutral")

    @property
    def priority_tone(self):
        return PRIORITY_TONES.get(self.priority, "neutral")


STATUS_TONES = {
    Ticket.Status.OPEN: "info",
    Ticket.Status.REOPENED: "warning",
    Ticket.Status.IN_PROGRESS: "accent",
    Ticket.Status.WAITING: "warning",
    Ticket.Status.RESOLVED: "good",
    Ticket.Status.CLOSED: "neutral",
}

PRIORITY_TONES = {
    Ticket.Priority.LOW: "neutral",
    Ticket.Priority.MEDIUM: "info",
    Ticket.Priority.HIGH: "warning",
    Ticket.Priority.URGENT: "critical",
}


class TicketComment(models.Model):
    """A message on a ticket: a reply visible to the user, or an internal note."""

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="comments",
    )

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    body = models.TextField()

    # Internal notes are only shown to the responsible team and administrators.
    is_internal = models.BooleanField(
        default=False,
    )

    # A reply that asks the user for more details (sets the ticket waiting).
    is_info_request = models.BooleanField(
        default=False,
    )

    created_at = models.DateTimeField(
        default=timezone.now,
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{'Note' if self.is_internal else 'Comment'} on {self.ticket.reference}"


def attachment_path(attachment, filename):
    # Never the user's name: a random name under the ticket's folder.
    extension = os.path.splitext(filename)[1].lower()[:16]

    return f"support/{attachment.ticket_id}/{uuid.uuid4().hex}{extension}"


class TicketAttachment(models.Model):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="attachments",
    )

    comment = models.ForeignKey(
        TicketComment,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="attachments",
    )

    file = models.FileField(
        upload_to=attachment_path,
        max_length=255,
    )

    original_name = models.CharField(
        max_length=255,
    )

    content_type = models.CharField(
        max_length=100,
        blank=True,
    )

    size = models.PositiveBigIntegerField(
        default=0,
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    # Follows the comment it was posted with.
    is_internal = models.BooleanField(
        default=False,
    )

    created_at = models.DateTimeField(
        default=timezone.now,
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return self.original_name

    @property
    def is_image(self):
        return self.content_type.startswith("image/")

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("support:attachment", args=[self.pk])


class TicketEvent(models.Model):
    """The ticket's history: who did what, when."""

    class Type(models.TextChoices):
        CREATED = "created", "Ticket created"
        ASSIGNED = "assigned", "Assigned"
        UNASSIGNED = "unassigned", "Unassigned"
        STATUS_CHANGED = "status_changed", "Status changed"
        PRIORITY_CHANGED = "priority_changed", "Priority changed"
        COMMENTED = "commented", "Reply added"
        NOTE_ADDED = "note_added", "Internal note added"
        INFO_REQUESTED = "info_requested", "Information requested"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"
        REOPENED = "reopened", "Reopened"
        ATTACHMENT_ADDED = "attachment_added", "Attachment added"
        TEAM_CHANGED = "team_changed", "Team changed"

    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="events",
    )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    type = models.CharField(
        max_length=30,
        choices=Type.choices,
    )

    details = models.JSONField(
        default=dict,
        blank=True,
    )

    created_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["type", "created_at"]),
        ]
        verbose_name = "ticket activity"
        verbose_name_plural = "ticket activity"

    def __str__(self):
        return f"{self.get_type_display()} on {self.ticket.reference}"

    @property
    def summary(self):
        """One line of details, e.g. "Open → In progress"."""

        d = self.details or {}

        if self.type == self.Type.ASSIGNED:
            if d.get("from"):
                return f"{d['from']} → {d.get('to', '')}"
            return d.get("to", "")

        if self.type == self.Type.UNASSIGNED:
            return d.get("from", "")

        if self.type in (self.Type.STATUS_CHANGED, self.Type.PRIORITY_CHANGED, self.Type.TEAM_CHANGED):
            return f"{d.get('from') or '—'} → {d.get('to') or '—'}"

        if self.type == self.Type.CREATED:
            return d.get("team") and f"routed to {d['team']}" or "no responsible team"

        if self.type == self.Type.ATTACHMENT_ADDED:
            return d.get("name", "")

        if self.type == self.Type.REOPENED and d.get("reason"):
            return d["reason"]

        return d.get("note", "")


class Notification(models.Model):
    """An in-app notification; the same message is also emailed."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="support_notifications",
    )

    ticket = models.ForeignKey(
        Ticket,
        null=True,
        on_delete=models.CASCADE,
        related_name="notifications",
    )

    kind = models.CharField(
        max_length=30,
    )

    title = models.CharField(
        max_length=200,
    )

    body = models.TextField(
        blank=True,
    )

    created_at = models.DateTimeField(
        default=timezone.now,
    )

    read_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "read_at"]),
        ]

    def __str__(self):
        return self.title

    @property
    def is_read(self):
        return self.read_at is not None
