"""Forms of the ticket system: sign-in, tickets, team work and /manage/."""

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.core.exceptions import ValidationError
from django.db.models import Case, F, IntegerField, Q, Value, When

from applications.models import Application
from users.validators import validate_password_strength

from . import access
from .models import Department, SupportSettings, Team, TeamMembership, Ticket, TicketEvent
from .services import validate_files


Status = Ticket.Status


# ---- Uploads -----------------------------------------------------------------

class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Several files from one input; each one is checked by validate_files()."""

    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_clean = super().clean

        if isinstance(data, (list, tuple)):
            return [single_clean(item, initial) for item in data]

        return [single_clean(data, initial)] if data else []


def attachment_field(config):
    allowed = ", ".join(sorted(config.allowed_extension_set()))

    return MultipleFileField(
        required=False,
        label="Attachments",
        help_text=f"Screenshots, logs or documents, up to {config.max_attachment_mb} MB each ({allowed}).",
        widget=MultipleFileInput(
            attrs={
                "accept": ",".join(f".{ext}" for ext in sorted(config.allowed_extension_set())),
                "data-max-bytes": config.max_attachment_bytes,
                "data-max-label": f"{config.max_attachment_mb} MB",
            }
        ),
    )


# ---- Account -----------------------------------------------------------------

class LoginForm(AuthenticationForm):

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request, *args, **kwargs)

        self.fields["username"].label = "Email"
        self.fields["username"].widget.attrs.update(
            {"autocomplete": "email", "autofocus": True, "type": "email", "placeholder": "you@jivo.in"}
        )
        self.fields["password"].widget.attrs.update({"autocomplete": "current-password"})

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)

        if settings.REQUIRE_VERIFIED_EMAIL and not user.is_verified:
            raise ValidationError(
                "Verify your email address first. Check your inbox for the verification link.",
                code="email_not_verified",
            )


class PortalPasswordChangeForm(PasswordChangeForm):

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("new_password2")

        # Django ran AUTH_PASSWORD_VALIDATORS; add the service's composition rules.
        if password and not self.has_error("new_password2"):
            try:
                validate_password_strength(password, self.user)
            except ValidationError as error:
                self.add_error("new_password2", error)

        return cleaned


# ---- Choice fields -----------------------------------------------------------

class NameChoiceField(forms.ModelChoiceField):

    def label_from_instance(self, obj):
        return obj.name


class PersonChoiceField(forms.ModelChoiceField):

    def label_from_instance(self, user):
        name = user.get_full_name()

        return f"{name} ({user.email})" if name else user.email


# Kept for the names used elsewhere.
AssigneeChoiceField = PersonChoiceField
TeamChoiceField = NameChoiceField


def catalog():
    """Active departments and the applications in them that accept tickets."""

    applications = (
        Application.objects
        .filter(is_active=True, tickets_enabled=True, department__is_active=True)
        .select_related("department")
        .order_by("department__name", "name")
    )

    departments = (
        Department.objects
        .filter(pk__in=applications.values("department"))
        .order_by("name")
    )

    return departments, applications


# ---- Tickets -----------------------------------------------------------------

class TicketForm(forms.Form):

    department = NameChoiceField(
        queryset=Department.objects.none(),
        empty_label="Choose a department",
        help_text="Who looks after the application you need help with.",
    )

    application = NameChoiceField(
        queryset=Application.objects.none(),
        empty_label="Choose an application",
    )

    subject = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "A short title for the problem", "autocomplete": "off"}),
    )

    description = forms.CharField(
        widget=forms.Textarea(
            attrs={"rows": 8, "placeholder": "What happened, what you expected, and the steps to reproduce it."}
        ),
        help_text="Include error messages and the time it happened.",
    )

    def __init__(self, *args, config=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.config = config or SupportSettings.load()
        departments, applications = catalog()

        self.fields["department"].queryset = departments
        self.fields["application"].queryset = applications
        self.fields["attachments"] = attachment_field(self.config)

    def clean(self):
        cleaned = super().clean()
        department = cleaned.get("department")
        application = cleaned.get("application")

        if department and application and application.department_id != department.pk:
            self.add_error("application", f"{application.name} doesn't belong to {department.name}.")

        return cleaned

    def clean_attachments(self):
        files = self.cleaned_data.get("attachments") or []
        validate_files(files, self.config)

        return files


class CommentForm(forms.Form):

    ACTIONS = ("reply", "note", "request_info")

    body = forms.CharField(
        label="Message",
        widget=forms.Textarea(attrs={"rows": 5, "placeholder": "Write a reply…"}),
    )

    action = forms.ChoiceField(
        choices=[(action, action) for action in ACTIONS],
        initial="reply",
        widget=forms.HiddenInput,
    )

    def __init__(self, *args, config=None, can_handle=False, **kwargs):
        super().__init__(*args, **kwargs)

        self.config = config or SupportSettings.load()
        self.can_handle = can_handle
        self.fields["attachments"] = attachment_field(self.config)

    def clean_action(self):
        action = self.cleaned_data["action"]

        if action != "reply" and not self.can_handle:
            raise ValidationError("Only the responsible team can do that.")

        return action

    def clean_attachments(self):
        files = self.cleaned_data.get("attachments") or []
        validate_files(files, self.config)

        return files


class AssignForm(forms.Form):

    assignee = PersonChoiceField(
        queryset=None,
        required=False,
        empty_label="Nobody (unassigned)",
    )

    note = forms.CharField(
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "Optional note"}),
    )

    def __init__(self, *args, ticket, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["assignee"].queryset = access.assignable_users(ticket)
        self.fields["assignee"].initial = ticket.assignee_id


SIMPLE_STATUS_CHOICES = [
    (Status.OPEN, "Open (back in the queue)"),
    (Status.IN_PROGRESS, "In progress"),
    (Status.WAITING, "Waiting for information"),
]


class StatusForm(forms.Form):

    status = forms.ChoiceField(choices=SIMPLE_STATUS_CHOICES)

    note = forms.CharField(
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "Optional note"}),
    )


class PriorityForm(forms.Form):

    priority = forms.ChoiceField(choices=Ticket.Priority.choices)


class ResolveForm(forms.Form):

    resolution = forms.CharField(
        label="Resolution summary",
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "What was the cause and what was done about it?"}),
    )


class CloseForm(forms.Form):

    note = forms.CharField(
        required=False,
        label="Note",
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "Optional, e.g. duplicate of JT-0042"}),
    )


class ReopenForm(forms.Form):

    reason = forms.CharField(
        label="Why is it being reopened?",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "What is still wrong?"}),
    )


class MoveTeamForm(forms.Form):

    team = NameChoiceField(
        queryset=Team.objects.filter(is_active=True).order_by("name"),
        empty_label="No team",
        required=False,
    )

    note = forms.CharField(
        required=False,
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "Optional note"}),
    )


# ---- Ticket lists ------------------------------------------------------------

def priority_rank():
    return Case(
        *[When(priority=value, then=Value(rank)) for value, rank in Ticket.PRIORITY_ORDER.items()],
        output_field=IntegerField(),
    )


def search_tickets(queryset, query):
    query = (query or "").strip()

    if not query:
        return queryset

    condition = Q(subject__icontains=query) | Q(description__icontains=query)
    number = Ticket.parse_reference(query)

    if number is not None:
        condition |= Q(pk=number)

    return queryset.filter(condition)


def filter_status(queryset, status):
    if status == "active":
        return queryset.active()

    if status == "new":
        return queryset.filter(status__in=Ticket.OPEN_STATUSES)

    if status == "done":
        return queryset.filter(status__in=Ticket.DONE_STATUSES)

    if status in Status.values:
        return queryset.filter(status=status)

    return queryset


STATUS_FILTER_CHOICES = [
    ("", "Any status"),
    ("active", "Active"),
    ("new", "New (open or reopened)"),
    *Status.choices,
    ("done", "Resolved or closed"),
]

SORTS = [
    ("newest", "Newest first"),
    ("oldest", "Oldest first"),
    ("updated", "Recently updated"),
    ("priority", "Priority"),
    ("due", "Due soonest"),
]


def sort_tickets(queryset, sort):
    if sort == "oldest":
        return queryset.order_by("created_at", "pk")

    if sort == "updated":
        return queryset.order_by("-updated_at", "-pk")

    if sort == "priority":
        return queryset.annotate(priority_rank=priority_rank()).order_by("priority_rank", "created_at")

    if sort == "due":
        return queryset.order_by(F("resolve_due_at").asc(nulls_last=True), "created_at")

    return queryset.order_by("-created_at", "-pk")


class TicketFilterForm(forms.Form):
    """Search and filters of the users' and teams' ticket lists (GET)."""

    q = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"type": "search", "placeholder": "Search ticket ID or subject"}),
    )
    status = forms.ChoiceField(required=False, choices=STATUS_FILTER_CHOICES)
    priority = forms.ChoiceField(required=False, choices=[("", "Any priority"), *Ticket.Priority.choices])
    application = forms.ModelChoiceField(
        required=False,
        queryset=Application.objects.filter(department__isnull=False).order_by("name"),
        empty_label="Any application",
    )
    sort = forms.ChoiceField(required=False, choices=SORTS)

    FILTER_FIELDS = ("q", "status", "priority", "application")

    @property
    def is_filtered(self):
        return any(self.data.get(name) for name in self.FILTER_FIELDS)

    def apply(self, queryset):
        data = self.cleaned_data if self.is_valid() else {}

        queryset = search_tickets(queryset, data.get("q"))
        queryset = filter_status(queryset, data.get("status"))

        if data.get("priority"):
            queryset = queryset.filter(priority=data["priority"])

        if data.get("application"):
            queryset = queryset.filter(application=data["application"])

        return sort_tickets(queryset, data.get("sort"))


SLA_CHOICES = [
    ("", "Any SLA state"),
    ("overdue", "Overdue"),
    ("response", "First response overdue"),
    ("resolution", "Resolution overdue"),
    ("ok", "Within SLA"),
]


class ManageTicketFilterForm(TicketFilterForm):
    """Every filter of the administrators' ticket table."""

    department = forms.ModelChoiceField(required=False, queryset=Department.objects.order_by("name"), empty_label="Any department")
    team = forms.ChoiceField(required=False)
    assignee = forms.ChoiceField(required=False)
    sla = forms.ChoiceField(required=False, choices=SLA_CHOICES)
    created_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    created_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    updated_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    updated_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    per_page = forms.TypedChoiceField(required=False, coerce=int, empty_value=25, choices=[(25, "25 per page"), (50, "50 per page"), (100, "100 per page")])

    FILTER_FIELDS = (
        "q", "status", "priority", "application", "department", "team", "assignee", "sla",
        "created_from", "created_to", "updated_from", "updated_to",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["application"].queryset = Application.objects.order_by("name")
        self.fields["team"].choices = [
            ("", "Any team"),
            ("none", "No team"),
            *[(str(team.pk), team.name) for team in Team.objects.order_by("name")],
        ]
        people = (
            get_user_model().objects
            .filter(Q(team_memberships__isnull=False) | Q(assigned_tickets__isnull=False))
            .distinct()
            .order_by("first_name", "last_name", "email")
        )
        self.fields["assignee"].choices = [
            ("", "Anyone or nobody"),
            ("none", "Unassigned"),
            ("me", "Me"),
            *[(str(person.pk), person.get_full_name() or person.email) for person in people],
        ]
        self.user = user

    def apply(self, queryset):
        from django.utils import timezone

        queryset = super().apply(queryset)
        data = self.cleaned_data if self.is_valid() else {}

        if data.get("department"):
            queryset = queryset.filter(department=data["department"])

        team = data.get("team")
        if team == "none":
            queryset = queryset.filter(team__isnull=True)
        elif team:
            queryset = queryset.filter(team_id=team)

        assignee = data.get("assignee")
        if assignee == "none":
            queryset = queryset.filter(assignee__isnull=True)
        elif assignee == "me" and self.user is not None:
            queryset = queryset.filter(assignee=self.user)
        elif assignee and assignee != "me":
            queryset = queryset.filter(assignee_id=assignee)

        now = timezone.now()
        sla = data.get("sla")

        if sla == "overdue":
            queryset = queryset.overdue(now)
        elif sla == "response":
            queryset = queryset.active().filter(first_response_at__isnull=True, response_due_at__lt=now)
        elif sla == "resolution":
            queryset = queryset.active().filter(resolve_due_at__lt=now)
        elif sla == "ok":
            queryset = queryset.active().exclude(pk__in=Ticket.objects.overdue(now).values("pk"))

        for field, name in (("created_at", "created"), ("updated_at", "updated")):
            if data.get(f"{name}_from"):
                queryset = queryset.filter(**{f"{field}__date__gte": data[f"{name}_from"]})
            if data.get(f"{name}_to"):
                queryset = queryset.filter(**{f"{field}__date__lte": data[f"{name}_to"]})

        return queryset

    @property
    def per_page(self):
        return (self.cleaned_data.get("per_page") if self.is_valid() else None) or 25


class BulkActionForm(forms.Form):
    """The ticket table's bulk actions; each needs its own parameter."""

    ACTIONS = [
        ("assign", "Assign to…"),
        ("priority", "Change priority…"),
        ("status", "Change status…"),
        ("team", "Move to team…"),
        ("close", "Close"),
        ("reopen", "Reopen"),
    ]

    action = forms.ChoiceField(choices=ACTIONS)
    tickets = forms.ModelMultipleChoiceField(queryset=Ticket.objects.all(), widget=forms.MultipleHiddenInput)
    assignee = PersonChoiceField(
        queryset=get_user_model().objects.filter(is_active=True, team_memberships__isnull=False).distinct().order_by("first_name", "email"),
        required=False,
        empty_label="Nobody (unassign)",
    )
    priority = forms.ChoiceField(required=False, choices=Ticket.Priority.choices)
    status = forms.ChoiceField(required=False, choices=SIMPLE_STATUS_CHOICES)
    team = NameChoiceField(queryset=Team.objects.filter(is_active=True).order_by("name"), required=False, empty_label="No team")
    note = forms.CharField(required=False, max_length=200, label="Note or reason")

    def clean(self):
        cleaned = super().clean()
        action = cleaned.get("action")

        if action == "reopen" and not (cleaned.get("note") or "").strip():
            self.add_error("note", "Say why the tickets are reopened.")

        if action in ("priority", "status") and not cleaned.get(action):
            self.add_error(action, "Choose a value.")

        return cleaned


# ---- Management ------------------------------------------------------------------

class DepartmentForm(forms.ModelForm):

    default_team = NameChoiceField(
        queryset=Team.objects.filter(is_active=True).order_by("name"),
        required=False,
        empty_label="None",
        help_text=Department._meta.get_field("default_team").help_text,
    )

    class Meta:
        model = Department
        fields = ["name", "description", "default_team", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        labels = {"is_active": "Active"}


class ApplicationRoutingForm(forms.ModelForm):
    """What the ticket system may change on an application: its routing."""

    department = NameChoiceField(queryset=Department.objects.order_by("name"), required=False, empty_label="None (not on the ticket form)")
    support_team = NameChoiceField(
        queryset=Team.objects.filter(is_active=True).order_by("name"),
        required=False,
        empty_label="None",
        label="Responsible team",
        help_text="New tickets go to this team. Existing tickets keep their team.",
    )

    class Meta:
        model = Application
        fields = ["department", "support_team", "tickets_enabled"]
        labels = {"tickets_enabled": "Accepts tickets"}


class ApplicationCreateForm(ApplicationRoutingForm):
    """
    Register an application for tickets only. It gets no API key and no
    users, so it changes nothing for sign-in; the slug is permanent because
    tokens would carry it if the application is integrated later.
    """

    class Meta(ApplicationRoutingForm.Meta):
        fields = ["name", "slug", "department", "support_team", "tickets_enabled"]
        help_texts = {"slug": "Lowercase identifier, e.g. jsap. It can't be changed later."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].required = True
        self.fields["department"].empty_label = "Choose a department"


class TeamEditForm(forms.ModelForm):

    class Meta:
        model = Team
        fields = ["name", "description", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        labels = {"is_active": "Active"}
        help_texts = {"is_active": Team._meta.get_field("is_active").help_text}


class PersonByEmailField(forms.EmailField):
    """An existing, active account, entered by email."""

    def clean(self, value):
        email = super().clean(value)

        if not email:
            return None

        user = get_user_model().objects.filter(email__iexact=email).first()

        if user is None:
            raise ValidationError(f"No account has the email {email}. Accounts are created in the auth admin panel.")

        if not user.is_active:
            raise ValidationError(f"{email} is deactivated.")

        return user


class MemberAddForm(forms.Form):

    person = PersonByEmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"placeholder": "name@jivo.in", "autocomplete": "off", "list": "people-suggestions"}),
    )
    role = forms.ChoiceField(choices=TeamMembership.Role.choices, initial=TeamMembership.Role.MEMBER)


class MemberRoleForm(forms.Form):

    role = forms.ChoiceField(choices=TeamMembership.Role.choices)


class ReassignForm(forms.Form):
    """Move a member's active tickets of the team to someone else, or back to
    the queue (the member is away or leaving)."""

    member = PersonChoiceField(queryset=None, label="Tickets of", empty_label="Choose a member")
    to = PersonChoiceField(queryset=None, required=False, empty_label="Nobody (back to the queue)", label="Move them to")

    def __init__(self, *args, team, **kwargs):
        super().__init__(*args, **kwargs)

        members = access.team_member_users(team.pk)
        self.fields["member"].queryset = (
            get_user_model().objects
            .filter(Q(pk__in=members.values("pk")) | Q(assigned_tickets__team=team, assigned_tickets__status__in=Ticket.ACTIVE_STATUSES))
            .distinct()
            .order_by("first_name", "email")
        )
        self.fields["to"].queryset = members

    def clean(self):
        cleaned = super().clean()

        if cleaned.get("member") and cleaned.get("to") and cleaned["member"].pk == cleaned["to"].pk:
            self.add_error("to", "Choose someone else.")

        return cleaned


class AdministratorGrantForm(forms.Form):

    person = PersonByEmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"placeholder": "name@jivo.in", "autocomplete": "off", "list": "people-suggestions"}),
    )


class PeopleFilterForm(forms.Form):

    ROLES = [
        ("", "Everyone"),
        ("admins", "Administrators"),
        ("managers", "Team managers"),
        ("members", "Team members"),
        ("dashboards", "Can see the JI dashboards"),
        ("users", "Users only"),
    ]

    q = forms.CharField(required=False, widget=forms.TextInput(attrs={"type": "search", "placeholder": "Search name, email or employee code"}))
    role = forms.ChoiceField(required=False, choices=ROLES)

    def apply(self, queryset):
        from .models import SupportAdministrator

        data = self.cleaned_data if self.is_valid() else {}
        query = (data.get("q") or "").strip()

        if query:
            queryset = queryset.filter(
                Q(email__icontains=query) | Q(first_name__icontains=query)
                | Q(last_name__icontains=query) | Q(employee_code__icontains=query)
            )

        admins = Q(is_superuser=True) | Q(pk__in=SupportAdministrator.objects.values("user"))
        role = data.get("role")

        if role == "admins":
            queryset = queryset.filter(admins)
        elif role == "managers":
            queryset = queryset.filter(team_memberships__role=TeamMembership.Role.MANAGER).distinct()
        elif role == "members":
            queryset = queryset.filter(team_memberships__isnull=False).distinct()
        elif role == "dashboards":
            queryset = queryset.filter(dashboard_access_q()).distinct()
        elif role == "users":
            queryset = queryset.filter(team_memberships__isnull=True).exclude(admins)

        return queryset


def dashboard_access_q():
    """Who sees the JI dashboards: the permission directly, through a staff
    role, or as a superuser (boards.access)."""

    codename = {"permissions__content_type__app_label": "boards", "permissions__codename": "view_dashboards"}

    return (
        Q(is_superuser=True)
        | Q(user_permissions__content_type__app_label="boards", user_permissions__codename="view_dashboards")
        | Q(**{f"groups__{key}": value for key, value in codename.items()})
    )


class SettingsForm(forms.ModelForm):

    class Meta:
        model = SupportSettings
        exclude = ["updated_at"]
        widgets = {"allowed_extensions": forms.TextInput()}


class ActivityFilterForm(forms.Form):

    q = forms.CharField(required=False, widget=forms.TextInput(attrs={"type": "search", "placeholder": "Ticket ID, subject or person"}))
    type = forms.ChoiceField(required=False, choices=[("", "Any action"), *TicketEvent.Type.choices])
    since = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    until = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    FILTER_FIELDS = ("q", "type", "since", "until")

    @property
    def is_filtered(self):
        return any(self.data.get(name) for name in self.FILTER_FIELDS)

    def apply(self, queryset):
        data = self.cleaned_data if self.is_valid() else {}
        query = (data.get("q") or "").strip()

        if query:
            condition = (
                Q(ticket__subject__icontains=query) | Q(actor__email__icontains=query)
                | Q(actor__first_name__icontains=query) | Q(actor__last_name__icontains=query)
            )
            number = Ticket.parse_reference(query)

            if number is not None:
                condition |= Q(ticket_id=number)

            queryset = queryset.filter(condition)

        if data.get("type"):
            queryset = queryset.filter(type=data["type"])

        if data.get("since"):
            queryset = queryset.filter(created_at__date__gte=data["since"])

        if data.get("until"):
            queryset = queryset.filter(created_at__date__lte=data["until"])

        return queryset
