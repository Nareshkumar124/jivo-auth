"""
The ticket system at /: sign-in, the role-based dashboard, raising and
following tickets, the team workspace and notifications. Administration
lives in support.manage_views (/manage/). None of it uses the Django admin.
"""

from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponseNotAllowed, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.cache import never_cache

from applications.throttling import LoginIPRateThrottle, LoginRateThrottle, ScopedRateThrottle
from audit.models import AuditEvent
from audit.services import record

from . import access, services
from .attachments import download_content_type
from .dashboard import build_dashboard, team_overview, team_workload
from .forms import (
    AssignForm,
    CloseForm,
    CommentForm,
    LoginForm,
    MoveTeamForm,
    PortalPasswordChangeForm,
    PriorityForm,
    ReassignForm,
    ReopenForm,
    ResolveForm,
    StatusForm,
    TicketFilterForm,
    TicketForm,
)
from .models import Notification, SupportSettings, Team, Ticket, TicketAttachment
from .navigation import portal_context
from .services import SupportError


PAGE_SIZE = 25


def throttled(request, scope):
    """Whether the signed-in user exceeded the API rate for `scope`."""

    return not ScopedRateThrottle().allow_request(
        request,
        SimpleNamespace(throttle_scope=scope),
    )


def login_throttled(request):
    """The same per-account and per-IP limits as the login API."""

    # The throttles read the email from request.data (a DRF request).
    request.data = request.POST

    return not all(
        throttle().allow_request(request, None)
        for throttle in (LoginRateThrottle, LoginIPRateThrottle)
    )


# ---- Sign in ---------------------------------------------------------------

@method_decorator(never_cache, name="dispatch")
class LoginView(auth_views.LoginView):

    template_name = "support/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def get_default_redirect_url(self):
        return reverse("support:home")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "title": "Sign in",
                "action": self.request.get_full_path(),
            }
        )

        return context

    def post(self, request, *args, **kwargs):
        if login_throttled(request):
            form = self.get_form()
            form.add_error(None, "Too many sign-in attempts. Try again in a minute.")

            return self.render_to_response(self.get_context_data(form=form), status=429)

        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)

        record(
            AuditEvent.Type.LOGIN_SUCCEEDED,
            user=form.get_user(),
            request=self.request,
            device="Support portal",
        )

        return response

    def form_invalid(self, form):
        email = (form.data.get("username") or "").strip().lower()

        if email and form.data.get("password"):
            codes = {error.code for error in form.non_field_errors().as_data()}
            reason = "email_not_verified" if "email_not_verified" in codes else (
                "no_active_account" if "inactive" in codes else "invalid_credentials"
            )

            record(
                AuditEvent.Type.LOGIN_FAILED,
                user=form.get_user() if reason != "invalid_credentials" else None,
                email=email,
                request=self.request,
                reason=reason,
                device="Support portal",
            )

        return super().form_invalid(form)


class LogoutView(auth_views.LogoutView):

    next_page = reverse_lazy("support:home")


class PortalMixin(LoginRequiredMixin):

    login_url = reverse_lazy("support:login")

    def render(self, template, title="", status=200, **context):
        return render(
            self.request,
            template,
            portal_context(self.request, title=title, **context),
            status=status,
        )

    def config(self):
        if not hasattr(self.request, "_support_config"):
            self.request._support_config = SupportSettings.load()

        return self.request._support_config

    def paginate(self, queryset, per_page=PAGE_SIZE):
        paginator = Paginator(queryset, per_page)

        return paginator.get_page(self.request.GET.get("page"))


class PasswordChangeView(PortalMixin, auth_views.PasswordChangeView):

    template_name = "support/password_change.html"
    form_class = PortalPasswordChangeForm
    success_url = reverse_lazy("support:home")

    def get_context_data(self, **kwargs):
        return {**portal_context(self.request, title="Change password"), **super().get_context_data(**kwargs)}

    def form_valid(self, form):
        messages.success(self.request, "Your password was changed. Other devices were signed out.")

        return super().form_valid(form)


# ---- Home and tickets ------------------------------------------------------

class HomeView(View):
    """/: the sign-in page for visitors, the dashboard once signed in."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return LoginView.as_view()(request, *args, **kwargs)

        if request.method not in ("GET", "HEAD"):
            return HttpResponseNotAllowed(["GET", "HEAD"])

        return render(
            request,
            "support/dashboard.html",
            portal_context(request, title="Home", dashboard=build_dashboard(request.user)),
        )


class TicketCreateView(PortalMixin, View):

    template_name = "support/ticket_form.html"

    def get(self, request):
        form = TicketForm(config=self.config())

        return self.render(self.template_name, title="Raise ticket", form=form, catalog_empty=not form.fields["department"].queryset.exists())

    def post(self, request):
        form = TicketForm(request.POST, request.FILES, config=self.config())

        if throttled(request, "support_ticket"):
            form.add_error(None, "You've created many tickets in a short time. Try again in a while.")

            return self.render(self.template_name, title="Raise ticket", form=form, status=429)

        if not form.is_valid():
            return self.render(self.template_name, title="Raise ticket", form=form, status=400)

        data = form.cleaned_data

        try:
            ticket = services.create_ticket(
                created_by=request.user,
                department=data["department"],
                application=data["application"],
                subject=data["subject"],
                description=data["description"],
                files=data["attachments"],
                config=self.config(),
            )
        except ValidationError as error:
            form.add_error("attachments", error)

            return self.render(self.template_name, title="Raise ticket", form=form, status=400)

        messages.success(
            request,
            f"Ticket {ticket.reference} was created"
            + (f" and sent to {ticket.team.name}." if ticket.team else ". An administrator will route it to a team."),
        )

        return redirect(ticket)


class MyTicketsView(PortalMixin, View):

    def get(self, request):
        form = TicketFilterForm(request.GET)
        tickets = form.apply(Ticket.objects.filter(created_by=request.user).with_related())
        page = self.paginate(tickets)

        counts = Ticket.objects.filter(created_by=request.user).aggregate(
            total=Count("pk"),
            active=Count("pk", filter=Q(status__in=Ticket.ACTIVE_STATUSES)),
            waiting=Count("pk", filter=Q(status=Ticket.Status.WAITING)),
            done=Count("pk", filter=Q(status__in=Ticket.DONE_STATUSES)),
        )

        return self.render(
            "support/ticket_list.html",
            title="My tickets",
            form=form,
            page=page,
            counts=counts,
            is_filtered=form.is_filtered,
            now=timezone.now(),
        )


class TicketDetailView(PortalMixin, View):

    def get(self, request, pk):
        ticket = get_object_or_404(Ticket.objects.with_related(), pk=pk)
        user = request.user

        if not access.can_view(user, ticket):
            raise PermissionDenied

        config = self.config()
        can_handle = access.can_handle(user, ticket)
        internal_ok = access.can_view_internal(user, ticket)

        comments = ticket.comments.select_related("author").prefetch_related("attachments")
        attachments = ticket.attachments.filter(comment__isnull=True)

        if not internal_ok:
            comments = comments.filter(is_internal=False)
            attachments = attachments.filter(is_internal=False)

        events = ticket.events.select_related("actor")

        if not can_handle:
            events = events.exclude(type="note_added")

        events = events[:50]

        # Marks this ticket's notifications read: the person is looking at it.
        Notification.objects.filter(user=user, ticket=ticket, read_at__isnull=True).update(read_at=timezone.now())

        permissions = {
            "handle": can_handle,
            "internal": internal_ok,
            "comment": access.can_comment(user, ticket),
            "claim": access.can_claim(user, ticket),
            "assign": access.can_assign(user, ticket, config),
            "status": access.can_change_status(user, ticket),
            "priority": access.can_change_priority(user, ticket, config),
            "team": access.can_change_team(user, ticket),
            "request_info": access.can_request_info(user, ticket),
            "resolve": access.can_resolve(user, ticket),
            "close": access.can_close(user, ticket),
            "reopen": access.can_reopen(user, ticket, config),
        }

        forms = {
            "comment": CommentForm(config=config, can_handle=can_handle),
            "assign": AssignForm(ticket=ticket) if permissions["assign"] else None,
            "status": StatusForm(initial={"status": ticket.status}) if permissions["status"] else None,
            "priority": PriorityForm(initial={"priority": ticket.priority}) if permissions["priority"] else None,
            "team": MoveTeamForm(initial={"team": ticket.team_id}) if permissions["team"] else None,
            "resolve": ResolveForm() if permissions["resolve"] else None,
            "close": CloseForm() if permissions["close"] else None,
            "reopen": ReopenForm() if permissions["reopen"] else None,
        }

        return self.render(
            "support/ticket_detail.html",
            title=f"{ticket.reference} · {ticket.subject}",
            ticket=ticket,
            comments=comments,
            attachments=attachments,
            events=events,
            can=permissions,
            forms=forms,
            is_creator=access.is_creator(user, ticket),
            now=timezone.now(),
            overdue=ticket.overdue_kind(),
        )


class TicketActionView(PortalMixin, View):
    """Base for the POST-only actions on a ticket."""

    http_method_names = ["post"]

    def post(self, request, pk):
        self.ticket = get_object_or_404(Ticket.objects.with_related(), pk=pk)

        if not access.can_view(request.user, self.ticket):
            raise PermissionDenied

        try:
            self.perform(request, self.ticket)
        except SupportError as error:
            messages.error(request, str(error))
        except ValidationError as error:
            for message in error.messages:
                messages.error(request, message)

        return redirect(self.ticket)

    def perform(self, request, ticket):
        raise NotImplementedError

    def invalid(self, form):
        for field, errors in form.errors.items():
            label = form.fields[field].label if field in form.fields else ""

            for error in errors:
                messages.error(self.request, f"{label}: {error}" if label else error)


class CommentView(TicketActionView):

    def perform(self, request, ticket):
        user = request.user
        can_handle = access.can_handle(user, ticket)
        form = CommentForm(request.POST, request.FILES, config=self.config(), can_handle=can_handle)

        if not form.is_valid():
            return self.invalid(form)

        if throttled(request, "support_comment"):
            raise SupportError("You're sending messages too quickly. Try again in a few minutes.")

        action = form.cleaned_data["action"]
        body = form.cleaned_data["body"]
        files = form.cleaned_data["attachments"]

        if action == "request_info":
            if not access.can_request_info(user, ticket):
                raise PermissionDenied

            services.request_information(ticket, user, body, files=files, config=self.config())
            messages.success(request, "Asked the user for more information. The ticket is now waiting.")

        elif action == "note":
            if not access.can_view_internal(user, ticket):
                raise PermissionDenied

            services.add_comment(ticket, user, body, internal=True, files=files, config=self.config())
            messages.success(request, "Internal note added. Only the team and administrators can see it.")

        else:
            if not access.can_comment(user, ticket):
                raise SupportError("This ticket is closed. Reopen it to continue the conversation.")

            services.add_comment(ticket, user, body, files=files, config=self.config())
            messages.success(request, "Your reply was posted.")


class ClaimView(TicketActionView):

    def perform(self, request, ticket):
        if not access.can_claim(request.user, ticket):
            raise PermissionDenied

        services.claim(ticket, request.user)
        messages.success(request, f"{ticket.reference} is yours now.")


class AssignView(TicketActionView):

    def perform(self, request, ticket):
        if not access.can_assign(request.user, ticket, self.config()):
            raise PermissionDenied

        form = AssignForm(request.POST, ticket=ticket)

        if not form.is_valid():
            return self.invalid(form)

        assignee = form.cleaned_data["assignee"]
        services.assign(ticket, request.user, assignee, note=form.cleaned_data["note"])

        messages.success(
            request,
            f"Assigned to {assignee.get_full_name() or assignee.email}." if assignee else "The ticket is unassigned.",
        )


class StatusView(TicketActionView):

    def perform(self, request, ticket):
        if not access.can_change_status(request.user, ticket):
            raise PermissionDenied

        form = StatusForm(request.POST)

        if not form.is_valid():
            return self.invalid(form)

        services.change_status(ticket, request.user, form.cleaned_data["status"], note=form.cleaned_data["note"])
        messages.success(request, f"Status set to {Ticket.Status(form.cleaned_data['status']).label.lower()}.")


class PriorityView(TicketActionView):

    def perform(self, request, ticket):
        if not access.can_change_priority(request.user, ticket, self.config()):
            raise PermissionDenied

        form = PriorityForm(request.POST)

        if not form.is_valid():
            return self.invalid(form)

        services.change_priority(ticket, request.user, form.cleaned_data["priority"], config=self.config())
        messages.success(request, f"Priority set to {Ticket.Priority(form.cleaned_data['priority']).label.lower()}.")


class TeamView(TicketActionView):

    def perform(self, request, ticket):
        if not access.can_change_team(request.user, ticket):
            raise PermissionDenied

        form = MoveTeamForm(request.POST)

        if not form.is_valid():
            return self.invalid(form)

        team = form.cleaned_data["team"]
        services.change_team(ticket, request.user, team, note=form.cleaned_data["note"])
        messages.success(request, f"Moved to {team.name}." if team else "The ticket has no team now.")


class ResolveView(TicketActionView):

    def perform(self, request, ticket):
        if not access.can_resolve(request.user, ticket):
            raise PermissionDenied

        form = ResolveForm(request.POST)

        if not form.is_valid():
            return self.invalid(form)

        services.resolve(ticket, request.user, form.cleaned_data["resolution"])
        messages.success(request, f"{ticket.reference} is resolved. The user has been told.")


class CloseView(TicketActionView):

    def perform(self, request, ticket):
        if not access.can_close(request.user, ticket):
            raise PermissionDenied

        form = CloseForm(request.POST)

        if not form.is_valid():
            return self.invalid(form)

        services.close(ticket, request.user, note=form.cleaned_data["note"])
        messages.success(request, f"{ticket.reference} is closed.")


class ReopenView(TicketActionView):

    def perform(self, request, ticket):
        if not access.can_reopen(request.user, ticket, self.config()):
            raise PermissionDenied

        form = ReopenForm(request.POST)

        if not form.is_valid():
            return self.invalid(form)

        services.reopen(ticket, request.user, form.cleaned_data["reason"], config=self.config())
        messages.success(request, f"{ticket.reference} is open again.")


# ---- Attachments -----------------------------------------------------------

class AttachmentView(PortalMixin, View):

    def get(self, request, pk):
        attachment = get_object_or_404(TicketAttachment.objects.select_related("ticket"), pk=pk)
        ticket = attachment.ticket

        if not access.can_view(request.user, ticket):
            raise PermissionDenied

        if attachment.is_internal and not access.can_view_internal(request.user, ticket):
            raise PermissionDenied

        try:
            handle = attachment.file.open("rb")
        except FileNotFoundError as exc:
            raise Http404("The file is no longer on the server.") from exc

        response = FileResponse(
            handle,
            content_type=download_content_type(attachment),
            as_attachment=not attachment.is_image,
            filename=attachment.original_name,
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        response["Cache-Control"] = "private, max-age=300"

        return response


# ---- Team workspace --------------------------------------------------------

class TeamRequiredMixin(PortalMixin):

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not access.is_team_member(request.user):
            raise PermissionDenied

        return super().dispatch(request, *args, **kwargs)


class TeamIndexView(TeamRequiredMixin, View):
    """The teams the person belongs to, with their queues."""

    def get(self, request):
        teams = Team.objects.filter(pk__in=access.team_ids(request.user))

        if teams.count() == 1:
            return redirect("support:team-overview", teams.get().pk)

        return self.render("support/team_index.html", title="Team overview", teams=team_workload(teams))


class TeamOverviewView(PortalMixin, View):
    """One team: counts, performance, members' workload. Managers and
    administrators also get the reassign tool."""

    def get(self, request, pk):
        team = get_object_or_404(Team, pk=pk)

        if not access.can_view_team(request.user, team.pk):
            raise PermissionDenied

        can_manage = access.can_manage_team(request.user, team.pk)

        return self.render(
            "support/team_overview.html",
            title=team.name,
            can_manage=can_manage,
            reassign_form=ReassignForm(team=team) if can_manage else None,
            **team_overview(team, request.user),
        )


class TeamReassignView(PortalMixin, View):
    """POST: move a member's active tickets of the team elsewhere."""

    http_method_names = ["post"]

    def post(self, request, pk):
        team = get_object_or_404(Team, pk=pk)

        if not access.can_manage_team(request.user, team.pk):
            raise PermissionDenied

        form = ReassignForm(request.POST, team=team)

        if form.is_valid():
            member, to = form.cleaned_data["member"], form.cleaned_data["to"]
            moved, skipped = services.reassign_member_tickets(team, request.user, member, to)

            target = to.get_full_name() or to.email if to else "the queue"
            messages.success(request, f"Moved {moved} ticket(s) of {member.get_full_name() or member.email} to {target}.")

            for problem in skipped[:5]:
                messages.warning(request, problem)
        else:
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)

        next_url = request.POST.get("next", "")

        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return HttpResponseRedirect(next_url)

        return redirect("support:team-overview", team.pk)


TEAM_PRESETS = {
    "available": ("Available tickets", "Unassigned tickets your teams can claim."),
    "mine": ("My assigned tickets", "Tickets assigned to you."),
    "all": ("Team tickets", "Every ticket routed to your teams."),
    "resolved": ("Resolved tickets", "Resolved and closed tickets of your teams."),
}


class TeamTicketsView(TeamRequiredMixin, View):

    def get(self, request, preset):
        if preset not in TEAM_PRESETS:
            raise Http404

        title, blurb = TEAM_PRESETS[preset]
        teams = access.team_ids(request.user)
        tickets = Ticket.objects.filter(team_id__in=teams).with_related()

        if preset == "available":
            tickets = tickets.unassigned()
        elif preset == "mine":
            tickets = Ticket.objects.filter(assignee=request.user).with_related()
            if not request.GET.get("status"):
                tickets = tickets.active()
        elif preset == "resolved":
            tickets = tickets.filter(status__in=Ticket.DONE_STATUSES)

        form = TicketFilterForm(request.GET)
        tickets = form.apply(tickets)

        return self.render(
            "support/team_tickets.html",
            title=title,
            blurb=blurb,
            preset=preset,
            form=form,
            page=self.paginate(tickets),
            is_filtered=form.is_filtered,
            now=timezone.now(),
        )


# ---- Notifications ---------------------------------------------------------

class NotificationsView(PortalMixin, View):

    def get(self, request):
        notifications = Notification.objects.filter(user=request.user).select_related("ticket")

        return self.render(
            "support/notifications.html",
            title="Notifications",
            page=self.paginate(notifications, 30),
        )

    def post(self, request):
        queryset = Notification.objects.filter(user=request.user, read_at__isnull=True)

        if request.POST.get("id"):
            queryset = queryset.filter(pk=request.POST["id"])

        queryset.update(read_at=timezone.now())

        next_url = request.POST.get("next", "")

        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            return HttpResponseRedirect(next_url)

        return redirect("support:notifications")
