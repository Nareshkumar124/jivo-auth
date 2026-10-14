"""
Administration of the ticket system at /manage/: every ticket, departments,
applications' routing, teams and members, people and roles, reports,
activity and settings. Only ticket administrators (support.access.is_admin)
get in; everyone else gets 403, whatever URL they type. The Django admin
at /admin/ is the auth system's and has none of this.
"""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View

from applications.models import Application

from . import access, services
from .dashboard import member_workload, team_workload
from .forms import (
    ActivityFilterForm,
    AdministratorGrantForm,
    ApplicationCreateForm,
    ApplicationRoutingForm,
    BulkActionForm,
    DepartmentForm,
    ManageTicketFilterForm,
    MemberAddForm,
    MemberRoleForm,
    PeopleFilterForm,
    ReassignForm,
    SettingsForm,
    TeamEditForm,
)
from .models import (
    Department,
    SupportAdministrator,
    SupportSettings,
    Team,
    TeamMembership,
    Ticket,
    TicketEvent,
)
from .reports import ReportFilterForm, bounds, build_report, export_summary, export_tickets, filtered_tickets
from .services import SupportError
from .views import PortalMixin


ACTIVE = Ticket.ACTIVE_STATUSES


class AdminRequiredMixin(PortalMixin):

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not access.is_admin(request.user):
            raise PermissionDenied

        return super().dispatch(request, *args, **kwargs)

    def form_errors(self, form):
        for field, errors in form.errors.items():
            label = form.fields[field].label if field in form.fields else ""

            for error in errors:
                messages.error(self.request, f"{label}: {error}" if label and field != "__all__" else error)


# ---- Tickets -------------------------------------------------------------------

class ManageTicketsView(AdminRequiredMixin, View):

    def get(self, request):
        form = ManageTicketFilterForm(request.GET, user=request.user)
        tickets = form.apply(Ticket.objects.with_related())

        if request.GET.get("export") == "csv":
            return export_tickets(tickets)

        page = self.paginate(tickets, form.per_page)

        return self.render(
            "support/manage/tickets.html",
            title="All tickets",
            form=form,
            page=page,
            is_filtered=form.is_filtered,
            bulk_actions=BulkActionForm.ACTIONS,
            export_query=_with(request.GET, export="csv", page=None),
            now=timezone.now(),
        )


def _with(params, **changes):
    query = params.copy()

    for key, value in changes.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = value

    return query.urlencode()


class BulkActionView(AdminRequiredMixin, View):
    """POST from the ticket table: a confirmation page first, then the action
    on the confirming POST (confirm=yes). Each ticket goes through the
    services, so the rules, history and notifications are the usual ones."""

    http_method_names = ["post"]

    LABELS = {
        "assign": "Assign",
        "priority": "Change priority",
        "status": "Change status",
        "team": "Move to a team",
        "close": "Close",
        "reopen": "Reopen",
    }

    def post(self, request):
        form = BulkActionForm(request.POST)
        back = request.POST.get("back") or reverse("support:manage-tickets")

        if not request.POST.getlist("tickets"):
            messages.warning(request, "Select at least one ticket first.")
            return redirect(back if back.startswith("/") else reverse("support:manage-tickets"))

        confirming = request.POST.get("confirm") == "yes"

        if not confirming or not form.is_valid():
            if confirming:
                self.form_errors(form)

            return self.render(
                "support/manage/confirm_bulk.html",
                title=self.LABELS.get(request.POST.get("action"), "Bulk action"),
                form=form if form.is_bound else BulkActionForm(),
                action=request.POST.get("action"),
                tickets=Ticket.objects.filter(pk__in=request.POST.getlist("tickets")).with_related()[:200],
                count=Ticket.objects.filter(pk__in=request.POST.getlist("tickets")).count(),
                back=back,
            )

        data = form.cleaned_data
        action = data["action"]
        actor = request.user
        note = (data.get("note") or "").strip()

        operations = {
            "assign": lambda t: services.assign(t, actor, data.get("assignee"), note=note),
            "priority": lambda t: services.change_priority(t, actor, data["priority"]),
            "status": lambda t: services.change_status(t, actor, data["status"], note=note),
            "team": lambda t: services.change_team(t, actor, data.get("team"), note=note),
            "close": lambda t: services.close(t, actor, note=note),
            "reopen": lambda t: services.reopen(t, actor, note),
        }

        done, skipped = 0, []

        for ticket in data["tickets"]:
            try:
                operations[action](ticket)
            except SupportError as error:
                skipped.append(f"{ticket.reference}: {error}")
                continue

            done += 1

        messages.success(request, f"{self.LABELS[action]}: {done} ticket(s) updated.")

        if skipped:
            messages.warning(request, f"Skipped {len(skipped)}: " + "; ".join(skipped[:5]) + (" …" if len(skipped) > 5 else ""))

        return redirect(back if back.startswith("/") else reverse("support:manage-tickets"))


# ---- Departments -------------------------------------------------------------------

class DepartmentListView(AdminRequiredMixin, View):

    def get(self, request):
        departments = (
            Department.objects
            .select_related("default_team")
            .prefetch_related(Prefetch("applications", queryset=Application.objects.order_by("name")))
            .annotate(
                pending=Count("tickets", filter=Q(tickets__status__in=ACTIVE), distinct=True),
                total=Count("tickets", distinct=True),
            )
            .order_by("-is_active", "-pending", "name")
        )

        return self.render("support/manage/departments.html", title="Departments", departments=departments)


class DepartmentFormView(AdminRequiredMixin, View):

    def get_object(self, pk):
        return get_object_or_404(Department, pk=pk) if pk else None

    def get(self, request, pk=None):
        department = self.get_object(pk)

        return self.show(DepartmentForm(instance=department), department)

    def post(self, request, pk=None):
        department = self.get_object(pk)
        form = DepartmentForm(request.POST, instance=department)

        if not form.is_valid():
            return self.show(form, department, status=400)

        saved = form.save()
        messages.success(request, f"{'Saved' if department else 'Created'} {saved.name}.")

        return redirect("support:manage-department", saved.pk)

    def show(self, form, department, status=200):
        context = {"form": form, "department": department}

        if department is not None:
            context["applications"] = (
                department.applications
                .select_related("support_team")
                .annotate(pending=Count("tickets", filter=Q(tickets__status__in=ACTIVE), distinct=True))
                .order_by("name")
            )
            context["counts"] = department.tickets.aggregate(
                total=Count("pk"),
                pending=Count("pk", filter=Q(status__in=ACTIVE)),
            )

        return self.render(
            "support/manage/department_form.html",
            title=department.name if department else "New department",
            status=status,
            **context,
        )


class DepartmentToggleView(AdminRequiredMixin, View):

    http_method_names = ["post"]

    def post(self, request, pk):
        department = get_object_or_404(Department, pk=pk)
        department.is_active = not department.is_active
        department.save(update_fields=["is_active"])

        messages.success(
            request,
            f"{department.name} is {'active again' if department.is_active else 'deactivated: users can no longer pick it'}.",
        )

        return redirect(request.POST.get("next") or reverse("support:manage-departments"))


# ---- Applications ---------------------------------------------------------------

class ApplicationListView(AdminRequiredMixin, View):

    ROUTING = {
        "routed": Q(department__isnull=False, support_team__isnull=False),
        "no_team": Q(tickets_enabled=True, department__isnull=False, support_team__isnull=True),
        "no_department": Q(department__isnull=True),
        "off": Q(tickets_enabled=False),
    }

    def get(self, request):
        applications = (
            Application.objects
            .select_related("department", "support_team", "department__default_team")
            .annotate(
                pending=Count("tickets", filter=Q(tickets__status__in=ACTIVE), distinct=True),
                total=Count("tickets", distinct=True),
            )
            .order_by("name")
        )

        query = request.GET.get("q", "").strip()
        if query:
            applications = applications.filter(Q(name__icontains=query) | Q(slug__icontains=query))

        if request.GET.get("department"):
            applications = applications.filter(department_id=request.GET["department"]) if request.GET["department"].isdigit() else applications

        if request.GET.get("team"):
            applications = applications.filter(support_team_id=request.GET["team"]) if request.GET["team"].isdigit() else applications

        routing = request.GET.get("routing", "")
        if routing in self.ROUTING:
            applications = applications.filter(self.ROUTING[routing])

        return self.render(
            "support/manage/applications.html",
            title="Applications",
            applications=applications,
            departments=Department.objects.order_by("name"),
            teams=Team.objects.order_by("name"),
            filters={"q": query, "department": request.GET.get("department", ""), "team": request.GET.get("team", ""), "routing": routing},
            is_filtered=bool(query or request.GET.get("department") or request.GET.get("team") or routing),
        )


class ApplicationCreateView(AdminRequiredMixin, View):

    def get(self, request):
        initial = {}

        if request.GET.get("department", "").isdigit():
            initial["department"] = request.GET["department"]

        return self.render("support/manage/application_form.html", title="New application", form=ApplicationCreateForm(initial=initial), application=None)

    def post(self, request):
        form = ApplicationCreateForm(request.POST)

        if not form.is_valid():
            return self.render("support/manage/application_form.html", title="New application", form=form, application=None, status=400)

        application = form.save()
        messages.success(request, f"Added {application.name}. It accepts tickets now; sign-in access is managed separately.")

        return redirect("support:manage-applications")


class ApplicationEditView(AdminRequiredMixin, View):

    def get(self, request, pk):
        application = get_object_or_404(Application, pk=pk)

        return self.show(ApplicationRoutingForm(instance=application), application)

    def post(self, request, pk):
        application = get_object_or_404(Application, pk=pk)
        previous_team = application.support_team
        form = ApplicationRoutingForm(request.POST, instance=application)

        if not form.is_valid():
            return self.show(form, application, status=400)

        application = form.save()
        message = f"Saved {application.name}."

        if previous_team != application.support_team:
            message += " New tickets follow the new team; existing tickets keep theirs."

        messages.success(request, message)

        return redirect("support:manage-applications")

    def show(self, form, application, status=200):
        counts = application.tickets.aggregate(
            total=Count("pk"),
            pending=Count("pk", filter=Q(status__in=ACTIVE)),
            with_old_team=Count("pk", filter=Q(status__in=ACTIVE) & ~Q(team=application.support_team)) if application.support_team_id else Count("pk", filter=Q(status__in=ACTIVE, team__isnull=False)),
        )

        return self.render(
            "support/manage/application_form.html",
            title=application.name,
            form=form,
            application=application,
            counts=counts,
            status=status,
        )


# ---- Teams ---------------------------------------------------------------

class TeamListView(AdminRequiredMixin, View):

    def get(self, request):
        teams = team_workload(Team.objects.prefetch_related("applications"))

        return self.render("support/manage/teams.html", title="Teams and members", teams=teams)


class TeamCreateView(AdminRequiredMixin, View):

    def get(self, request):
        return self.render("support/manage/team_form.html", title="New team", form=TeamEditForm())

    def post(self, request):
        form = TeamEditForm(request.POST)

        if not form.is_valid():
            return self.render("support/manage/team_form.html", title="New team", form=form, status=400)

        team = form.save()
        messages.success(request, f"Created {team.name}. Add its members below.")

        return redirect("support:manage-team", team.pk)


class TeamDetailView(AdminRequiredMixin, View):

    def get(self, request, pk):
        team = get_object_or_404(Team, pk=pk)

        return self.show(team, TeamEditForm(instance=team))

    def post(self, request, pk):
        team = get_object_or_404(Team, pk=pk)
        form = TeamEditForm(request.POST, instance=team)

        if not form.is_valid():
            return self.show(team, form, status=400)

        form.save()
        messages.success(request, f"Saved {team.name}.")

        return redirect("support:manage-team", team.pk)

    def show(self, team, form, status=200):
        return self.render(
            "support/manage/team_detail.html",
            title=team.name,
            team=team,
            form=form,
            workload=team_workload(Team.objects.filter(pk=team.pk))[0],
            members=member_workload({team.pk}),
            applications=team.applications.select_related("department").order_by("name"),
            add_form=MemberAddForm(),
            reassign_form=ReassignForm(team=team),
            roles=TeamMembership.Role.choices,
            people=get_user_model().objects.filter(is_active=True).exclude(team_memberships__team=team).order_by("email").values_list("email", flat=True)[:500],
            status=status,
        )


class MemberAddView(AdminRequiredMixin, View):

    http_method_names = ["post"]

    def post(self, request, pk):
        team = get_object_or_404(Team, pk=pk)
        form = MemberAddForm(request.POST)

        if form.is_valid():
            person = form.cleaned_data["person"]

            try:
                services.add_member(team, person, form.cleaned_data["role"], actor=request.user)
            except SupportError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, f"Added {person.get_full_name() or person.email} to {team.name}.")
        else:
            self.form_errors(form)

        return redirect("support:manage-team", team.pk)


class MemberUpdateView(AdminRequiredMixin, View):
    """POST: change a member's role, or remove them (remove=1)."""

    http_method_names = ["post"]

    def post(self, request, pk, membership_id):
        membership = get_object_or_404(TeamMembership.objects.select_related("user", "team"), pk=membership_id, team_id=pk)
        name = membership.user.get_full_name() or membership.user.email

        if request.POST.get("remove"):
            returned = services.remove_member(membership, request.user)
            messages.success(
                request,
                f"Removed {name} from {membership.team.name}."
                + (f" {returned} active ticket(s) went back to the queue." if returned else ""),
            )

            return redirect("support:manage-team", pk)

        form = MemberRoleForm(request.POST)

        if form.is_valid():
            services.set_member_role(membership, form.cleaned_data["role"])
            messages.success(request, f"{name} is now {membership.get_role_display().lower()} of {membership.team.name}.")
        else:
            self.form_errors(form)

        return redirect("support:manage-team", pk)


# ---- People and roles -----------------------------------------------------------

class PeopleView(AdminRequiredMixin, View):

    def get(self, request):
        form = PeopleFilterForm(request.GET)
        people = form.apply(get_user_model().objects.filter(is_active=True))
        people = (
            people
            .annotate(
                is_ticket_admin=Exists(SupportAdministrator.objects.filter(user=OuterRef("pk"))),
                raised=Count("created_tickets", distinct=True),
                handling=Count("assigned_tickets", filter=Q(assigned_tickets__status__in=ACTIVE), distinct=True),
            )
            .prefetch_related(Prefetch("team_memberships", queryset=TeamMembership.objects.select_related("team").order_by("team__name")))
            .order_by("first_name", "last_name", "email")
        )

        return self.render(
            "support/manage/people.html",
            title="People and roles",
            form=form,
            page=self.paginate(people, 30),
            is_filtered=bool(request.GET.get("q") or request.GET.get("role")),
            grant_form=AdministratorGrantForm(),
            counts={
                "admins": SupportAdministrator.objects.filter(user__is_active=True).count() + get_user_model().objects.filter(is_superuser=True, is_active=True).count(),
                "managers": TeamMembership.objects.filter(role=TeamMembership.Role.MANAGER).values("user").distinct().count(),
                "members": TeamMembership.objects.values("user").distinct().count(),
            },
        )


class AdministratorView(AdminRequiredMixin, View):
    """POST: grant (by email) or revoke (by user id) the administrator role."""

    http_method_names = ["post"]

    def post(self, request):
        try:
            if request.POST.get("revoke"):
                person = get_object_or_404(get_user_model(), pk=request.POST["revoke"])
                services.revoke_administrator(person, request.user)
                messages.success(request, f"{person.get_full_name() or person.email} is no longer an administrator.")
            else:
                form = AdministratorGrantForm(request.POST)

                if not form.is_valid():
                    self.form_errors(form)
                    return redirect("support:manage-people")

                person = form.cleaned_data["person"]
                services.grant_administrator(person, request.user)
                messages.success(request, f"{person.get_full_name() or person.email} is now a ticket administrator.")
        except SupportError as error:
            messages.error(request, str(error))

        return redirect(request.POST.get("next") if (request.POST.get("next") or "").startswith("/") else reverse("support:manage-people"))


# ---- Reports, activity, settings ------------------------------------------------

class ReportsView(AdminRequiredMixin, View):

    def get(self, request):
        form = ReportFilterForm(request.GET)

        if form.is_valid():
            data = form.cleaned_data
        else:
            defaults = ReportFilterForm({})
            defaults.is_valid()
            data = defaults.cleaned_data

        export = request.GET.get("export")

        if export == "tickets":
            first, last = bounds(data["start"], data["end"])
            tickets = filtered_tickets(data).filter(created_at__range=(first, last)).order_by("created_at")

            return export_tickets(tickets, f"tickets-{data['start']}-{data['end']}.csv")

        report = build_report(data)

        if export == "summary":
            return export_summary(report)

        return self.render(
            "support/manage/reports.html",
            title="Reports and analytics",
            form=form,
            report=report,
            query=_with(request.GET, export=None),
        )


class ActivityView(AdminRequiredMixin, View):

    def get(self, request):
        form = ActivityFilterForm(request.GET)
        events = form.apply(TicketEvent.objects.select_related("ticket", "actor"))

        return self.render(
            "support/manage/activity.html",
            title="Activity history",
            form=form,
            page=self.paginate(events, 50),
            is_filtered=form.is_filtered,
        )


class SettingsView(AdminRequiredMixin, View):

    def get(self, request):
        return self.render("support/manage/settings.html", title="Settings", form=SettingsForm(instance=SupportSettings.load()))

    def post(self, request):
        form = SettingsForm(request.POST, instance=SupportSettings.load())

        if not form.is_valid():
            return self.render("support/manage/settings.html", title="Settings", form=form, status=400)

        form.save()
        messages.success(request, "Settings saved. New deadlines apply to tickets created or reprioritised from now on.")

        return redirect("support:manage-settings")
