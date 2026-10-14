import csv
import io
import os
import shutil
import tempfile
from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from applications.models import Application
from audit.models import AuditEvent
from users.models import User

from . import access, services
from .models import (
    Department,
    Notification,
    SupportAdministrator,
    SupportSettings,
    Team,
    TeamMembership,
    Ticket,
    TicketAttachment,
    TicketComment,
    TicketEvent,
)
from .services import SupportError


PASSWORD = "Str0ng!Pass"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

Status = Ticket.Status

MEDIA = tempfile.mkdtemp(prefix="jivo-support-tests-")


def user(email, **extra):
    return User.objects.create_user(email=email, password=PASSWORD, is_verified=True, **extra)


@override_settings(MEDIA_ROOT=MEDIA)
class SupportTestCase(TestCase):
    """A department with three applications, a team with a manager and a
    member, another team, a ticket administrator, a superuser and two
    ordinary users."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        cache.clear()

        self.it = Department.objects.create(name="IT Department")
        self.oms_team = Team.objects.create(name="OMS Team")
        self.other_team = Team.objects.create(name="JSAP Team")

        self.ravi = user("ravi@jivo.in", first_name="Ravi", last_name="Kumar")
        self.meera = user("meera@jivo.in", first_name="Meera")
        self.priya = user("priya@jivo.in", first_name="Priya")
        TeamMembership.objects.create(team=self.oms_team, user=self.ravi, role=TeamMembership.Role.MANAGER)
        TeamMembership.objects.create(team=self.oms_team, user=self.meera)
        TeamMembership.objects.create(team=self.other_team, user=self.priya)

        self.oms = Application.objects.create(slug="oms", name="Jivo OMS", department=self.it, support_team=self.oms_team)
        self.jsap = Application.objects.create(slug="jsap", name="JSAP", department=self.it, support_team=self.other_team)
        self.orphan = Application.objects.create(slug="orphan", name="Orphan app", department=self.it)

        self.alice = user("alice@jivo.in", first_name="Alice", last_name="Smith")
        self.bob = user("bob@jivo.in")

        # A ticket administrator needs neither staff status nor Django permissions.
        self.admin = user("admin@jivo.in")
        SupportAdministrator.objects.create(user=self.admin)

        self.superuser = User.objects.create_superuser(email="root@jivo.in", password=PASSWORD)

    def ticket(self, creator=None, application=None, **kwargs):
        return services.create_ticket(
            created_by=creator or self.alice,
            department=self.it,
            application=application or self.oms,
            subject=kwargs.pop("subject", "Can't download the invoice PDF"),
            description=kwargs.pop("description", "Clicking download shows a blank page."),
            **kwargs,
        )

    def events(self, ticket, type_):
        return ticket.events.filter(type=type_)

    def unread(self, who):
        return Notification.objects.filter(user=who, read_at__isnull=True)


# ---- Services -----------------------------------------------------------------

class RoutingTests(SupportTestCase):

    def test_ticket_goes_to_the_applications_team_with_sla_deadlines(self):
        with self.captureOnCommitCallbacks(execute=True):
            ticket = self.ticket()

        self.assertEqual(ticket.team, self.oms_team)
        self.assertEqual(ticket.status, Status.OPEN)
        self.assertEqual(ticket.priority, Ticket.Priority.MEDIUM)
        self.assertEqual(ticket.reference, f"JT-{ticket.pk:04d}")
        self.assertEqual(ticket.response_due_at, ticket.created_at + timedelta(hours=8))
        self.assertEqual(ticket.resolve_due_at, ticket.created_at + timedelta(hours=72))

        created = self.events(ticket, TicketEvent.Type.CREATED).get()
        self.assertEqual(created.actor, self.alice)
        self.assertEqual(created.details["team"], "OMS Team")

        # Both team members are told (one message, Bcc); the creator isn't.
        self.assertEqual({n.user for n in Notification.objects.filter(ticket=ticket)}, {self.ravi, self.meera})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(sorted(mail.outbox[0].bcc), ["meera@jivo.in", "ravi@jivo.in"])
        self.assertEqual(mail.outbox[0].to, [])
        self.assertIn(f"[{ticket.reference}]", mail.outbox[0].subject)
        self.assertIn(f"/tickets/{ticket.pk}/", mail.outbox[0].body)

    def test_department_default_team_catches_applications_without_one(self):
        self.it.default_team = self.other_team
        self.it.save()

        ticket = self.ticket(application=self.orphan)

        self.assertEqual(ticket.team, self.other_team)

    def test_no_team_when_nothing_is_configured(self):
        ticket = self.ticket(application=self.orphan)

        self.assertIsNone(ticket.team)
        self.assertEqual(Notification.objects.filter(ticket=ticket).count(), 0)

    def test_inactive_team_is_skipped(self):
        self.oms_team.is_active = False
        self.oms_team.save()

        self.assertIsNone(self.ticket().team)

    def test_changing_the_applications_team_keeps_existing_tickets(self):
        ticket = self.ticket()

        self.oms.support_team = self.other_team
        self.oms.save()

        ticket.refresh_from_db()
        self.assertEqual(ticket.team, self.oms_team)
        self.assertEqual(self.ticket().team, self.other_team)

    def test_team_email_can_be_turned_off(self):
        config = SupportSettings.load()
        config.notify_team_on_new_ticket = False
        config.save()

        with self.captureOnCommitCallbacks(execute=True):
            self.ticket()

        self.assertEqual(len(mail.outbox), 0)

    def test_parse_reference(self):
        for text, expected in (("JT-0042", 42), ("jt42", 42), ("#42", 42), ("42", 42), ("oms", None), ("", None)):
            self.assertEqual(Ticket.parse_reference(text), expected)


class AssignmentTests(SupportTestCase):

    def test_claim_takes_the_ticket_into_progress(self):
        ticket = self.ticket()

        with self.captureOnCommitCallbacks(execute=True):
            ticket = services.claim(ticket, self.meera)

        self.assertEqual(ticket.assignee, self.meera)
        self.assertEqual(ticket.status, Status.IN_PROGRESS)
        self.assertIsNotNone(ticket.assigned_at)
        self.assertEqual(self.events(ticket, TicketEvent.Type.ASSIGNED).get().details["to"], "Meera")
        self.assertTrue(self.events(ticket, TicketEvent.Type.STATUS_CHANGED).exists())
        # The creator hears the ticket is in progress; Meera claimed it herself.
        self.assertTrue(self.unread(self.alice).filter(kind="status").exists())
        self.assertFalse(self.unread(self.meera).filter(kind="assigned").exists())

    def test_second_claim_is_refused(self):
        ticket = services.claim(self.ticket(), self.meera)

        with self.assertRaisesMessage(SupportError, "Meera claimed this ticket first"):
            services.claim(ticket, self.ravi)

        ticket.refresh_from_db()
        self.assertEqual(ticket.assignee, self.meera)

    def test_only_the_responsible_team_can_claim(self):
        ticket = self.ticket()

        with self.assertRaises(SupportError):
            services.claim(ticket, self.priya)

        self.assertFalse(access.can_claim(self.alice, ticket))
        self.assertTrue(access.can_claim(self.admin, ticket))

    def test_assign_to_a_teammate_and_unassign(self):
        ticket = self.ticket()

        with self.captureOnCommitCallbacks(execute=True):
            ticket = services.assign(ticket, self.ravi, self.meera, note="knows this one")

        self.assertEqual(ticket.assignee, self.meera)
        self.assertEqual(ticket.status, Status.IN_PROGRESS)
        self.assertTrue(self.unread(self.meera).filter(kind="assigned").exists())

        ticket = services.assign(ticket, self.ravi, self.ravi)
        self.assertTrue(self.unread(self.meera).filter(kind="reassigned").exists())

        ticket = services.assign(ticket, self.ravi, None)
        self.assertIsNone(ticket.assignee)
        self.assertEqual(ticket.status, Status.OPEN)
        self.assertTrue(self.events(ticket, TicketEvent.Type.UNASSIGNED).exists())

    def test_assignee_must_belong_to_the_team(self):
        ticket = self.ticket()

        with self.assertRaisesMessage(SupportError, "isn't a member of OMS Team"):
            services.assign(ticket, self.ravi, self.priya)

        with self.assertRaisesMessage(SupportError, "isn't assigned to anyone"):
            services.assign(ticket, self.ravi, None)

    def test_members_may_not_assign_when_the_setting_is_off(self):
        config = SupportSettings.load()
        config.members_can_assign = False
        config.members_can_change_priority = False
        config.save()
        ticket = self.ticket()

        self.assertFalse(access.can_assign(self.meera, ticket, config))
        self.assertTrue(access.can_assign(self.ravi, ticket, config))   # manager
        self.assertTrue(access.can_assign(self.admin, ticket, config))
        self.assertFalse(access.can_change_priority(self.meera, ticket, config))
        self.assertTrue(access.can_claim(self.meera, ticket))

    def test_moving_to_another_team_drops_an_outside_assignee(self):
        ticket = services.claim(self.ticket(), self.meera)

        with self.captureOnCommitCallbacks(execute=True):
            ticket = services.change_team(ticket, self.admin, self.other_team)

        self.assertEqual(ticket.team, self.other_team)
        self.assertIsNone(ticket.assignee)
        self.assertEqual(ticket.status, Status.OPEN)
        self.assertTrue(self.unread(self.priya).filter(kind="team").exists())
        self.assertEqual(self.events(ticket, TicketEvent.Type.TEAM_CHANGED).get().summary, "OMS Team → JSAP Team")


class WorkflowTests(SupportTestCase):

    def test_status_transitions(self):
        ticket = self.ticket()

        ticket = services.change_status(ticket, self.meera, Status.IN_PROGRESS)
        # Taking it into progress made Meera the owner.
        self.assertEqual(ticket.assignee, self.meera)

        ticket = services.change_status(ticket, self.meera, Status.WAITING, note="asked for a screenshot")
        self.assertEqual(ticket.status, Status.WAITING)

        with self.assertRaises(SupportError):
            services.change_status(ticket, self.meera, Status.WAITING)

        with self.assertRaises(SupportError):
            services.change_status(ticket, self.meera, Status.RESOLVED)

        ticket = services.resolve(ticket, self.meera, "Cleared the cache.")

        with self.assertRaisesMessage(SupportError, "can't be set to in progress"):
            services.change_status(ticket, self.meera, Status.IN_PROGRESS)

    def test_request_information_and_the_users_reply(self):
        ticket = services.claim(self.ticket(), self.meera)

        with self.captureOnCommitCallbacks(execute=True):
            comment = services.request_information(ticket, self.meera, "Which browser?")

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Status.WAITING)
        self.assertIsNotNone(ticket.first_response_at)
        self.assertTrue(comment.is_info_request)
        self.assertFalse(comment.is_internal)
        self.assertTrue(self.unread(self.alice).filter(kind="info_requested").exists())
        self.assertIn("More information needed", mail.outbox[-1].subject)

        with self.captureOnCommitCallbacks(execute=True):
            services.add_comment(ticket, self.alice, "Chrome 130")

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Status.IN_PROGRESS)
        self.assertTrue(self.unread(self.meera).filter(kind="reply").exists())

    def test_users_reply_on_an_unassigned_waiting_ticket_reopens_the_queue(self):
        ticket = self.ticket()
        services.request_information(ticket, self.ravi, "Details?")

        with self.captureOnCommitCallbacks(execute=True):
            services.add_comment(ticket, self.alice, "Here you go")

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Status.OPEN)
        # Nobody owns it: the whole team is told.
        self.assertEqual({n.user for n in Notification.objects.filter(ticket=ticket, kind="reply")}, {self.ravi, self.meera})

    def test_team_reply_counts_as_first_response(self):
        ticket = self.ticket()

        with self.captureOnCommitCallbacks(execute=True):
            services.add_comment(ticket, self.ravi, "Looking into it.")

        ticket.refresh_from_db()
        self.assertIsNotNone(ticket.first_response_at)
        self.assertTrue(self.unread(self.alice).filter(kind="reply").exists())

    def test_internal_notes_are_for_the_team_only(self):
        ticket = services.claim(self.ticket(), self.meera)

        with self.assertRaises(SupportError):
            services.add_comment(ticket, self.alice, "sneaky", internal=True)

        note = services.add_comment(ticket, self.ravi, "Customer is VIP", internal=True)

        self.assertTrue(note.is_internal)
        self.assertTrue(self.events(ticket, TicketEvent.Type.NOTE_ADDED).exists())
        self.assertFalse(self.unread(self.alice).filter(kind="note").exists())
        self.assertTrue(self.unread(self.meera).filter(kind="note").exists())
        ticket.refresh_from_db()
        self.assertIsNone(ticket.first_response_at)

    def test_resolve_close_and_reopen(self):
        ticket = self.ticket()

        with self.captureOnCommitCallbacks(execute=True):
            ticket = services.resolve(ticket, self.meera, "Fixed the export.")

        self.assertEqual(ticket.status, Status.RESOLVED)
        self.assertEqual(ticket.assignee, self.meera)
        self.assertEqual(ticket.resolved_by, self.meera)
        self.assertIsNotNone(ticket.resolved_at)
        self.assertIn("Ticket resolved", mail.outbox[-1].subject)

        with self.assertRaises(SupportError):
            services.resolve(ticket, self.meera, "again")

        ticket = services.close(ticket, self.alice)
        self.assertEqual(ticket.status, Status.CLOSED)
        self.assertIsNotNone(ticket.closed_at)

        with self.assertRaises(SupportError):
            services.close(ticket, self.meera)

        with self.captureOnCommitCallbacks(execute=True):
            ticket = services.reopen(ticket, self.alice, "Still broken on Monday")

        self.assertEqual(ticket.status, Status.REOPENED)
        self.assertEqual(ticket.reopen_count, 1)
        self.assertIsNone(ticket.resolved_at)
        self.assertIsNone(ticket.closed_at)
        self.assertIsNone(ticket.first_response_at)
        self.assertEqual(ticket.resolution, "Fixed the export.")
        self.assertEqual(ticket.resolve_due_at, ticket.reopened_at + timedelta(hours=72))
        self.assertEqual(ticket.comments.last().body, "Still broken on Monday")
        self.assertTrue(self.unread(self.meera).filter(kind="reopened").exists())
        self.assertTrue(ticket.is_open)

    def test_user_reopen_window(self):
        config = SupportSettings.load()
        config.reopen_window_days = 3
        config.save()
        ticket = services.resolve(self.ticket(), self.meera, "Done")

        self.assertTrue(access.can_reopen(self.alice, ticket, config))

        Ticket.objects.filter(pk=ticket.pk).update(resolved_at=timezone.now() - timedelta(days=4))
        ticket.refresh_from_db()

        self.assertFalse(access.can_reopen(self.alice, ticket, config))
        self.assertTrue(access.can_reopen(self.meera, ticket, config))
        self.assertFalse(access.can_reopen(self.bob, ticket, config))

        with self.assertRaises(SupportError):
            services.reopen(ticket, self.alice, "late")

        config.users_can_reopen = False
        config.save()
        Ticket.objects.filter(pk=ticket.pk).update(resolved_at=timezone.now())
        ticket.refresh_from_db()
        self.assertFalse(access.can_reopen(self.alice, ticket, config))

    def test_priority_change_recomputes_deadlines(self):
        ticket = self.ticket()

        with self.captureOnCommitCallbacks(execute=True):
            ticket = services.change_priority(ticket, self.ravi, Ticket.Priority.URGENT)

        self.assertEqual(ticket.response_due_at, ticket.created_at + timedelta(hours=1))
        self.assertEqual(ticket.resolve_due_at, ticket.created_at + timedelta(hours=8))
        self.assertEqual(self.events(ticket, TicketEvent.Type.PRIORITY_CHANGED).get().summary, "Medium → Urgent")

        with self.assertRaises(SupportError):
            services.change_priority(ticket, self.ravi, Ticket.Priority.URGENT)

    def test_overdue(self):
        ticket = self.ticket()
        self.assertEqual(ticket.overdue_kind(), "")

        Ticket.objects.filter(pk=ticket.pk).update(response_due_at=timezone.now() - timedelta(hours=1))
        ticket.refresh_from_db()
        self.assertEqual(ticket.overdue_kind(), "response")
        self.assertEqual(Ticket.objects.overdue().count(), 1)

        Ticket.objects.filter(pk=ticket.pk).update(resolve_due_at=timezone.now() - timedelta(hours=1))
        ticket.refresh_from_db()
        self.assertEqual(ticket.overdue_kind(), "resolution")

        services.resolve(ticket, self.ravi, "done")
        self.assertEqual(Ticket.objects.overdue().count(), 0)

    def test_close_resolved_tickets_command(self):
        answered = services.resolve(self.ticket(subject="answered"), self.meera, "ok")
        silent = services.resolve(self.ticket(subject="silent"), self.meera, "ok")
        fresh = services.resolve(self.ticket(subject="fresh"), self.meera, "ok")

        old = timezone.now() - timedelta(days=10)
        Ticket.objects.filter(pk__in=[answered.pk, silent.pk]).update(resolved_at=old)
        TicketComment.objects.create(ticket=answered, author=self.alice, body="not yet", created_at=old + timedelta(days=1))

        out = io.StringIO()
        call_command("close_resolved_tickets", stdout=out)

        self.assertIn("Closed 1 ticket", out.getvalue())
        self.assertEqual(Ticket.objects.get(pk=silent.pk).status, Status.CLOSED)
        self.assertEqual(Ticket.objects.get(pk=answered.pk).status, Status.RESOLVED)
        self.assertEqual(Ticket.objects.get(pk=fresh.pk).status, Status.RESOLVED)
        self.assertIsNone(self.events(Ticket.objects.get(pk=silent.pk), TicketEvent.Type.CLOSED).get().actor)

        config = SupportSettings.load()
        config.auto_close_after_days = 0
        config.save()
        out = io.StringIO()
        call_command("close_resolved_tickets", stdout=out)
        self.assertIn("off", out.getvalue())


class AccessTests(SupportTestCase):

    def test_visibility(self):
        mine = self.ticket()
        theirs = self.ticket(creator=self.bob, application=self.jsap)

        self.assertEqual(set(access.visible_tickets(self.alice)), {mine})
        self.assertEqual(set(access.visible_tickets(self.meera)), {mine})
        self.assertEqual(set(access.visible_tickets(self.priya)), {theirs})
        self.assertEqual(set(access.visible_tickets(self.admin)), {mine, theirs})
        self.assertEqual(set(access.visible_tickets(self.superuser)), {mine, theirs})

        self.assertTrue(access.can_view(self.alice, mine))
        self.assertFalse(access.can_view(self.alice, theirs))
        self.assertFalse(access.can_handle(self.alice, mine))
        self.assertTrue(access.can_handle(self.ravi, mine))
        self.assertFalse(access.can_handle(self.priya, mine))
        self.assertTrue(access.can_handle(self.admin, theirs))

    def test_roles(self):
        self.assertEqual(access.role_label(self.alice), "User")
        self.assertEqual(access.role_label(self.meera), "Team member")
        self.assertEqual(access.role_label(self.admin), "Administrator")
        self.assertEqual(access.role_label(self.ravi), "Team manager")
        self.assertEqual(access.role_label(self.superuser), "Superuser")
        self.assertTrue(access.is_admin(self.superuser))
        self.assertFalse(access.is_admin(self.ravi))

    def test_inactive_team_members_lose_the_workspace(self):
        self.oms_team.is_active = False
        self.oms_team.save()

        self.assertFalse(access.is_team_member(self.meera))

    def test_user_close_only_when_resolved(self):
        ticket = self.ticket()

        self.assertFalse(access.can_close(self.alice, ticket))
        self.assertTrue(access.can_close(self.meera, ticket))

        services.resolve(ticket, self.meera, "done")
        ticket.refresh_from_db()
        self.assertTrue(access.can_close(self.alice, ticket))
        self.assertFalse(access.can_close(self.bob, ticket))


# ---- Portal -------------------------------------------------------------------

class PortalLoginTests(SupportTestCase):

    def test_root_shows_the_sign_in_page_to_visitors(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "support/login.html")
        self.assertContains(response, 'type="email"')
        self.assertContains(response, reverse("forgot-password-page"))
        self.assertNotContains(response, "admin/css")
        self.assertNotContains(response, 'id="sidenav"')

    def test_signing_in_at_the_root_lands_on_the_dashboard(self):
        response = self.client.post("/", {"username": "Alice@jivo.in", "password": PASSWORD})

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        page = self.client.get("/")
        self.assertTemplateUsed(page, "support/dashboard.html")
        self.assertContains(page, "Welcome back, Alice")

        event = AuditEvent.objects.get(type=AuditEvent.Type.LOGIN_SUCCEEDED)
        self.assertEqual(event.user, self.alice)
        self.assertEqual(event.details["device"], "Support portal")

    def test_login_page_keeps_the_next_url(self):
        url = reverse("support:ticket-create")

        response = self.client.get(url)
        self.assertRedirects(response, f"{reverse('support:login')}?next={url}", fetch_redirect_response=False)

        response = self.client.post(f"{reverse('support:login')}?next={url}", {"username": "alice@jivo.in", "password": PASSWORD, "next": url})
        self.assertRedirects(response, url, fetch_redirect_response=False)

    def test_unverified_user_is_refused(self):
        User.objects.filter(pk=self.alice.pk).update(is_verified=False)

        response = self.client.post("/", {"username": "alice@jivo.in", "password": PASSWORD})

        self.assertContains(response, "Verify your email address first", status_code=200)
        self.assertEqual(AuditEvent.objects.get(type=AuditEvent.Type.LOGIN_FAILED).details["reason"], "email_not_verified")

    def test_wrong_password_is_audited_and_throttled(self):
        for _ in range(5):
            response = self.client.post(reverse("support:login"), {"username": "alice@jivo.in", "password": "nope"})
            self.assertEqual(response.status_code, 200)

        self.assertEqual(AuditEvent.objects.filter(type=AuditEvent.Type.LOGIN_FAILED, email="alice@jivo.in").count(), 5)

        response = self.client.post(reverse("support:login"), {"username": "alice@jivo.in", "password": PASSWORD})
        self.assertEqual(response.status_code, 429)

    def test_logout_returns_to_the_sign_in_page(self):
        self.client.force_login(self.alice)

        response = self.client.post(reverse("support:logout"))

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertTemplateUsed(self.client.get("/"), "support/login.html")

    def test_navigation_follows_the_role(self):
        self.client.force_login(self.alice)
        page = self.client.get("/")
        self.assertContains(page, "Raise ticket")
        self.assertNotContains(page, "Available tickets")
        self.assertNotContains(page, "Reports and analytics")
        self.assertNotContains(page, "Auth admin")
        self.assertContains(page, '<span class="user-chip__role">User</span>', html=True)

        self.client.force_login(self.meera)
        page = self.client.get("/")
        self.assertContains(page, "Available tickets")
        self.assertContains(page, "Team overview")
        self.assertNotContains(page, "Reports and analytics")
        self.assertContains(page, '<span class="user-chip__role">Team member</span>', html=True)

        self.client.force_login(self.admin)
        page = self.client.get("/")
        self.assertContains(page, "Reports and analytics")
        self.assertContains(page, reverse("support:manage-people"))
        self.assertContains(page, '<span class="user-chip__role">Administrator</span>', html=True)
        # A ticket administrator who isn't staff gets no link into the auth admin.
        self.assertNotContains(page, "Auth admin")

        self.client.force_login(self.superuser)
        self.assertContains(self.client.get("/"), "Auth admin")

    def test_pages_use_their_own_layout(self):
        self.client.force_login(self.admin)

        for url in ("/", reverse("support:my-tickets"), reverse("support:manage-tickets")):
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertContains(page, "support/css/app.css")
                self.assertNotContains(page, "admin/css/base.css")
                self.assertNotContains(page, "adminpanel/css")

    def test_password_change(self):
        self.client.force_login(self.alice)

        response = self.client.post(
            reverse("support:password-change"),
            {"old_password": PASSWORD, "new_password1": "N3w!Password", "new_password2": "N3w!Password"},
        )

        self.assertRedirects(response, reverse("support:home"), fetch_redirect_response=False)
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password("N3w!Password"))

        response = self.client.post(
            reverse("support:password-change"),
            {"old_password": "N3w!Password", "new_password1": "weakweak", "new_password2": "weakweak"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "uppercase")


class DashboardTests(SupportTestCase):

    def test_user_sees_only_their_own_figures(self):
        self.ticket(subject="Mine")
        waiting = self.ticket(subject="Needs me")
        services.request_information(waiting, self.ravi, "Which order?")
        self.ticket(creator=self.bob, subject="Bob's")
        self.client.force_login(self.alice)

        page = self.client.get("/")
        dashboard = page.context["dashboard"]

        self.assertNotIn("team", dashboard)
        self.assertNotIn("admin", dashboard)
        tiles = {tile["label"]: tile["value"] for tile in dashboard["user"]["tiles"]}
        self.assertEqual(tiles["My open tickets"], 2)
        self.assertEqual(tiles["Waiting for my reply"], 1)
        self.assertContains(page, "Needs me")
        self.assertNotContains(page, "Bob's")

    def test_team_member_sees_the_queue(self):
        available = self.ticket(subject="Available one")
        mine = services.claim(self.ticket(subject="Mine"), self.meera)
        services.request_information(mine, self.meera, "?")
        self.ticket(application=self.jsap, subject="Other team's")
        self.client.force_login(self.meera)

        page = self.client.get("/")
        team = page.context["dashboard"]["team"]

        tiles = {tile["label"]: tile["value"] for tile in team["tiles"]}
        self.assertEqual(tiles["Available to claim"], 1)
        self.assertEqual(tiles["Assigned to me"], 1)
        self.assertEqual(tiles["Waiting for information"], 1)
        self.assertEqual([t.pk for t in team["available"]], [available.pk])
        self.assertNotContains(page, "Other team's")
        self.assertNotIn("manager", page.context["dashboard"])

    def test_manager_sees_team_performance(self):
        ticket = self.ticket()
        services.add_comment(ticket, self.meera, "On it")
        services.resolve(ticket, self.meera, "done")
        self.client.force_login(self.ravi)

        page = self.client.get("/")
        manager = page.context["dashboard"]["manager"]

        self.assertEqual([team.name for team in manager["teams"]], ["OMS Team"])
        self.assertEqual(manager["teams"][0].performance["resolved"], 1)
        self.assertContains(page, "Teams I manage")
        self.assertContains(page, "Who holds what")

    def test_admin_sees_the_whole_desk_and_every_tile_opens_a_list(self):
        self.ticket(subject="Open one")
        done = services.resolve(self.ticket(subject="Done one"), self.meera, "ok")
        services.close(done, self.alice)
        self.ticket(application=self.orphan)
        late = self.ticket(subject="Late one")
        Ticket.objects.filter(pk=late.pk).update(resolve_due_at=timezone.now() - timedelta(hours=2))
        self.client.force_login(self.admin)

        page = self.client.get("/")
        admin = page.context["dashboard"]["admin"]

        tiles = {tile["label"]: tile for tile in admin["tiles"]}
        self.assertEqual(tiles["Total tickets"]["value"], 4)
        self.assertEqual(tiles["New and open"]["value"], 3)
        self.assertEqual(tiles["Unassigned"]["value"], 3)
        self.assertEqual(tiles["Closed"]["value"], 1)
        self.assertEqual(tiles["Overdue"]["value"], 1)
        self.assertContains(page, "Active tickets without a team")
        self.assertContains(page, "Team workload")

        for tile in tiles.values():
            with self.subTest(tile=tile["label"]):
                response = self.client.get(tile["url"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["page"].paginator.count, tile["value"])

    def test_admin_dashboard_link_for_applications_without_a_team(self):
        self.client.force_login(self.admin)

        attention = {item["text"]: item for item in self.client.get("/").context["dashboard"]["admin"]["attention"]}
        item = attention["applications whose tickets can't be routed"]

        self.assertEqual(item["count"], 1)
        page = self.client.get(item["url"])
        self.assertEqual([app.slug for app in page.context["applications"]], ["orphan"])

class TicketSubmissionTests(SupportTestCase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.alice)

    def test_form_lists_departments_and_their_applications(self):
        response = self.client.get(reverse("support:ticket-create"))

        self.assertContains(response, "IT Department")
        self.assertContains(response, f'<option value="{self.oms.pk}" data-parent="{self.it.pk}">Jivo OMS</option>', html=True)
        self.assertContains(response, "data-depends-on")

    def test_submit_with_attachments(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("support:ticket-create"),
                {
                    "department": self.it.pk,
                    "application": self.oms.pk,
                    "subject": "  Blank PDF  ",
                    "description": "Steps: open order 1042, click download.",
                    "attachments": [
                        SimpleUploadedFile("screen.png", PNG, content_type="image/png"),
                        SimpleUploadedFile("../../etc/log.txt", b"error line", content_type="text/plain"),
                    ],
                },
            )

        ticket = Ticket.objects.get()
        self.assertRedirects(response, ticket.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(ticket.subject, "Blank PDF")
        self.assertEqual(ticket.team, self.oms_team)

        names = sorted(ticket.attachments.values_list("original_name", flat=True))
        self.assertEqual(names, ["log.txt", "screen.png"])
        image = ticket.attachments.get(original_name="screen.png")
        self.assertEqual(image.content_type, "image/png")
        self.assertTrue(image.file.name.startswith(f"support/{ticket.pk}/"))
        self.assertNotIn("screen", image.file.name)
        self.assertEqual(image.size, len(PNG))

        page = self.client.get(ticket.get_absolute_url())
        self.assertContains(page, "was created and sent to OMS Team")
        self.assertContains(page, "screen.png")
        self.assertEqual(len(mail.outbox), 1)

    def test_application_must_belong_to_the_department(self):
        other = Department.objects.create(name="Finance")

        response = self.client.post(
            reverse("support:ticket-create"),
            {"department": other.pk, "application": self.oms.pk, "subject": "x", "description": "y"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Ticket.objects.count(), 0)

    def test_inactive_applications_and_departments_are_not_offered(self):
        self.oms.is_active = False
        self.oms.save()

        response = self.client.post(
            reverse("support:ticket-create"),
            {"department": self.it.pk, "application": self.oms.pk, "subject": "x", "description": "y"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Select a valid choice", status_code=400)

    def test_bad_attachments_are_refused_before_anything_is_saved(self):
        # Messages are HTML-escaped on the page, so match around the apostrophes.
        cases = [
            (SimpleUploadedFile("run.exe", b"MZ", content_type="application/octet-stream"), "this file type"),
            (SimpleUploadedFile("fake.png", b"<html>", content_type="image/png"), "a valid PNG image"),
            (SimpleUploadedFile("empty.txt", b"", content_type="text/plain"), "is empty"),
            (SimpleUploadedFile("big.txt", b"x" * (10 * 1024 * 1024 + 1), content_type="text/plain"), "larger than 10 MB"),
        ]

        for uploaded, message in cases:
            with self.subTest(file=uploaded.name):
                response = self.client.post(
                    reverse("support:ticket-create"),
                    {"department": self.it.pk, "application": self.oms.pk, "subject": "x", "description": "y", "attachments": [uploaded]},
                )

                self.assertEqual(response.status_code, 400)
                self.assertContains(response, message, status_code=400)

        self.assertEqual(Ticket.objects.count(), 0)
        self.assertEqual(TicketAttachment.objects.count(), 0)

    def test_submission_is_rate_limited(self):
        from unittest.mock import patch

        from applications.throttling import ScopedRateThrottle

        data = {"department": self.it.pk, "application": self.oms.pk, "subject": "x", "description": "y"}

        # The throttle classes read the rates once, at import time.
        with patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"support_ticket": "2/hour"}):
            self.assertEqual(self.client.post(reverse("support:ticket-create"), data).status_code, 302)
            self.assertEqual(self.client.post(reverse("support:ticket-create"), data).status_code, 302)
            self.assertEqual(self.client.post(reverse("support:ticket-create"), data).status_code, 429)

        self.assertEqual(Ticket.objects.count(), 2)

    def test_my_tickets_lists_only_mine(self):
        mine = self.ticket()
        self.ticket(creator=self.bob, subject="Bob's problem")

        response = self.client.get(reverse("support:my-tickets"))

        self.assertContains(response, mine.reference)
        self.assertNotContains(response, "Bob's problem")

        response = self.client.get(reverse("support:my-tickets"), {"q": mine.reference, "status": "active"})
        self.assertContains(response, mine.reference)

        response = self.client.get(reverse("support:my-tickets"), {"status": "closed"})
        self.assertContains(response, "No tickets match")

    def test_empty_catalog_shows_a_message(self):
        Application.objects.update(department=None)

        response = self.client.get(reverse("support:ticket-create"))

        self.assertContains(response, "No applications accept tickets yet")


class TicketPageTests(SupportTestCase):

    def test_creator_sees_the_ticket_without_internal_notes(self):
        ticket = services.claim(self.ticket(), self.meera)
        services.add_comment(ticket, self.meera, "Public reply")
        note = services.add_comment(ticket, self.meera, "Secret note", internal=True, files=[SimpleUploadedFile("secret.txt", b"shh", content_type="text/plain")])
        hidden_file = note.attachments.get()
        self.client.force_login(self.alice)

        page = self.client.get(ticket.get_absolute_url())

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Public reply")
        self.assertNotContains(page, "Secret note")
        self.assertNotContains(page, "secret.txt")
        self.assertNotContains(page, "Internal note added")
        self.assertNotContains(page, "Claim ticket")
        self.assertContains(page, "Send reply")
        self.assertNotContains(page, "Add internal note")
        self.assertEqual(self.client.get(hidden_file.get_absolute_url()).status_code, 403)

    def test_other_users_get_403(self):
        ticket = self.ticket()
        public = services.add_attachment(ticket, SimpleUploadedFile("log.txt", b"x", content_type="text/plain"), uploaded_by=self.alice)

        for who in (self.bob, self.priya):
            self.client.force_login(who)
            self.assertEqual(self.client.get(ticket.get_absolute_url()).status_code, 403)
            self.assertEqual(self.client.get(public.get_absolute_url()).status_code, 403)
            self.assertEqual(self.client.post(reverse("support:ticket-comment", args=[ticket.pk]), {"body": "hi", "action": "reply"}).status_code, 403)
            self.assertEqual(self.client.post(reverse("support:ticket-claim", args=[ticket.pk])).status_code, 403)

    def test_team_member_sees_actions_and_notes(self):
        ticket = self.ticket()
        services.add_comment(ticket, self.ravi, "Secret", internal=True)
        self.client.force_login(self.meera)

        page = self.client.get(ticket.get_absolute_url())

        self.assertContains(page, "Claim ticket")
        self.assertContains(page, "Secret")
        self.assertContains(page, "Add internal note")
        self.assertContains(page, "Request information")
        self.assertContains(page, "Resolve ticket")
        self.assertContains(page, "Internal note added")

    def test_attachment_download_headers(self):
        ticket = self.ticket(files=[
            SimpleUploadedFile("screen.png", PNG, content_type="image/png"),
            SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain"),
        ])
        image = ticket.attachments.get(original_name="screen.png")
        text = ticket.attachments.get(original_name="notes.txt")
        self.client.force_login(self.alice)

        response = self.client.get(image.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("inline", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(b"".join(response.streaming_content), PNG)

        response = self.client.get(text.get_absolute_url())
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("notes.txt", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"hello")

        self.assertEqual(self.client.get(reverse("support:attachment", args=["00000000-0000-0000-0000-000000000000"])).status_code, 404)

    def test_actions_through_the_portal(self):
        ticket = self.ticket()
        self.client.force_login(self.meera)

        self.assertRedirects(self.client.post(reverse("support:ticket-claim", args=[ticket.pk])), ticket.get_absolute_url(), fetch_redirect_response=False)
        ticket.refresh_from_db()
        self.assertEqual(ticket.assignee, self.meera)

        self.client.post(reverse("support:ticket-priority", args=[ticket.pk]), {"priority": "high"})
        self.client.post(reverse("support:ticket-assign", args=[ticket.pk]), {"assignee": self.ravi.pk})
        self.client.post(
            reverse("support:ticket-comment", args=[ticket.pk]),
            {"body": "Need the order number", "action": "request_info", "attachments": [SimpleUploadedFile("ask.txt", b"?", content_type="text/plain")]},
        )
        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, "high")
        self.assertEqual(ticket.assignee, self.ravi)
        self.assertEqual(ticket.status, Status.WAITING)
        self.assertEqual(ticket.comments.get().attachments.count(), 1)

        self.client.force_login(self.alice)
        self.client.post(reverse("support:ticket-comment", args=[ticket.pk]), {"body": "Order 1042", "action": "reply"})
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Status.IN_PROGRESS)

        # A user can't smuggle an internal note or claim.
        self.client.post(reverse("support:ticket-comment", args=[ticket.pk]), {"body": "x", "action": "note"})
        self.assertEqual(ticket.comments.filter(is_internal=True).count(), 0)
        self.assertEqual(self.client.post(reverse("support:ticket-claim", args=[ticket.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("support:ticket-resolve", args=[ticket.pk]), {"resolution": "me"}).status_code, 403)

        self.client.force_login(self.ravi)
        self.client.post(reverse("support:ticket-resolve", args=[ticket.pk]), {"resolution": "Fixed the export job."})
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Status.RESOLVED)

        self.client.force_login(self.alice)
        page = self.client.get(ticket.get_absolute_url())
        self.assertContains(page, "Fixed the export job.")
        self.assertContains(page, "Close ticket")
        self.assertContains(page, "Reopen ticket")

        self.client.post(reverse("support:ticket-close", args=[ticket.pk]), {"note": ""})
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Status.CLOSED)

        self.client.post(reverse("support:ticket-reopen", args=[ticket.pk]), {"reason": "Broke again"})
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, Status.REOPENED)
        self.assertEqual(ticket.reopen_count, 1)

        self.assertEqual(
            list(ticket.events.order_by("created_at").values_list("type", flat=True)),
            ["created", "assigned", "status_changed", "priority_changed", "assigned", "info_requested", "status_changed", "commented", "status_changed", "resolved", "closed", "reopened"],
        )

    def test_admin_can_move_the_team_and_sees_the_same_page(self):
        ticket = self.ticket()
        self.client.force_login(self.admin)

        page = self.client.get(ticket.get_absolute_url())
        self.assertContains(page, "Responsible team")

        self.client.post(reverse("support:ticket-team", args=[ticket.pk]), {"team": self.other_team.pk})
        ticket.refresh_from_db()
        self.assertEqual(ticket.team, self.other_team)

        self.client.force_login(self.meera)
        self.assertEqual(self.client.post(reverse("support:ticket-team", args=[ticket.pk]), {"team": self.oms_team.pk}).status_code, 403)

    def test_failed_actions_show_a_message(self):
        ticket = services.claim(self.ticket(), self.meera)
        self.client.force_login(self.ravi)

        response = self.client.post(reverse("support:ticket-claim", args=[ticket.pk]), follow=True)

        self.assertEqual(response.status_code, 403)

        response = self.client.post(reverse("support:ticket-assign", args=[ticket.pk]), {"assignee": self.priya.pk}, follow=True)
        self.assertContains(response, "Select a valid choice")

    def test_viewing_marks_notifications_read(self):
        ticket = self.ticket()
        self.client.force_login(self.meera)
        self.assertEqual(self.unread(self.meera).count(), 1)

        self.client.get(ticket.get_absolute_url())

        self.assertEqual(self.unread(self.meera).count(), 0)


class TeamWorkspaceTests(SupportTestCase):

    def test_plain_users_are_refused(self):
        self.client.force_login(self.alice)

        for url in (
            reverse("support:team-index"),
            reverse("support:team-tickets", args=["available"]),
            reverse("support:team-overview", args=[self.oms_team.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_team_lists(self):
        available = self.ticket(subject="Available one")
        mine = services.claim(self.ticket(subject="Mine"), self.meera)
        other = self.ticket(application=self.jsap, subject="Other team's")
        self.client.force_login(self.meera)

        for preset, expected, unexpected in (
            ("available", available.reference, mine.reference),
            ("mine", mine.reference, available.reference),
            ("all", available.reference, other.reference),
        ):
            with self.subTest(preset=preset):
                response = self.client.get(reverse("support:team-tickets", args=[preset]))
                self.assertContains(response, expected)
                self.assertNotContains(response, unexpected)

        services.resolve(mine, self.meera, "done")
        response = self.client.get(reverse("support:team-tickets", args=["resolved"]))
        self.assertContains(response, mine.reference)

        self.assertEqual(self.client.get(reverse("support:team-tickets", args=["nope"])).status_code, 404)

    def test_team_index_goes_straight_to_a_single_team(self):
        self.client.force_login(self.meera)

        response = self.client.get(reverse("support:team-index"))

        self.assertRedirects(response, reverse("support:team-overview", args=[self.oms_team.pk]))

    def test_overview_shows_the_reassign_tool_to_managers_only(self):
        self.client.force_login(self.meera)
        page = self.client.get(reverse("support:team-overview", args=[self.oms_team.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Reassign a member")
        self.assertEqual(self.client.get(reverse("support:team-overview", args=[self.other_team.pk])).status_code, 403)

        self.client.force_login(self.ravi)
        page = self.client.get(reverse("support:team-overview", args=[self.oms_team.pk]))
        self.assertContains(page, "Reassign a member")

    def test_manager_reassigns_a_members_tickets(self):
        first = services.claim(self.ticket(subject="First"), self.meera)
        second = services.claim(self.ticket(subject="Second"), self.meera)
        url = reverse("support:team-reassign", args=[self.oms_team.pk])

        self.client.force_login(self.meera)
        self.assertEqual(self.client.post(url, {"member": self.meera.pk, "to": self.ravi.pk}).status_code, 403)

        self.client.force_login(self.ravi)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {"member": self.meera.pk, "to": self.ravi.pk}, follow=True)

        self.assertContains(response, "Moved 2 ticket(s)")
        self.assertEqual(set(Ticket.objects.filter(pk__in=[first.pk, second.pk]).values_list("assignee", flat=True)), {self.ravi.pk})
        self.assertTrue(self.unread(self.meera).filter(kind="reassigned").exists())

        self.client.post(url, {"member": self.ravi.pk, "to": ""})
        self.assertEqual(Ticket.objects.filter(assignee__isnull=True, status=Ticket.Status.OPEN).count(), 2)

    def test_filters_and_sorting(self):
        low = self.ticket(subject="Low one")
        urgent = self.ticket(subject="Urgent one")
        services.change_priority(urgent, self.ravi, Ticket.Priority.URGENT)
        self.client.force_login(self.meera)

        response = self.client.get(reverse("support:team-tickets", args=["all"]), {"sort": "priority"})
        self.assertEqual([t.pk for t in response.context["page"].object_list], [urgent.pk, low.pk])

        response = self.client.get(reverse("support:team-tickets", args=["all"]), {"priority": "medium"})
        self.assertEqual([t.pk for t in response.context["page"].object_list], [low.pk])

        response = self.client.get(reverse("support:team-tickets", args=["all"]), {"q": "urgent"})
        self.assertEqual([t.pk for t in response.context["page"].object_list], [urgent.pk])

    def test_pagination(self):
        for index in range(30):
            self.ticket(subject=f"Ticket {index}")
        self.client.force_login(self.meera)

        response = self.client.get(reverse("support:team-tickets", args=["all"]))
        self.assertEqual(len(response.context["page"].object_list), 25)
        self.assertContains(response, "?page=2")

        response = self.client.get(reverse("support:team-tickets", args=["all"]), {"page": 2})
        self.assertEqual(len(response.context["page"].object_list), 5)


class NotificationPageTests(SupportTestCase):

    def test_list_and_mark_read(self):
        ticket = self.ticket()
        self.client.force_login(self.ravi)

        page = self.client.get(reverse("support:notifications"))
        self.assertContains(page, "New ticket for OMS Team")
        self.assertContains(page, "Mark all as read")
        self.assertContains(page, 'class="header-count"')

        self.client.post(reverse("support:notifications"))
        self.assertEqual(self.unread(self.ravi).count(), 0)

        note = Notification.objects.create(user=self.ravi, ticket=ticket, kind="x", title="Another")
        response = self.client.post(reverse("support:notifications"), {"id": note.pk, "next": "/tickets/"})
        self.assertRedirects(response, "/tickets/", fetch_redirect_response=False)
        note.refresh_from_db()
        self.assertIsNotNone(note.read_at)

        # Never an open redirect.
        response = self.client.post(reverse("support:notifications"), {"next": "https://evil.example/"})
        self.assertRedirects(response, reverse("support:notifications"), fetch_redirect_response=False)


# ---- Separation from the auth admin ------------------------------------------------

class AuthAdminSeparationTests(SupportTestCase):

    def test_django_admin_has_no_ticket_pages(self):
        self.client.force_login(self.superuser)

        for path in ("/admin/support/ticket/", "/admin/support/team/", "/admin/support/department/", "/admin/support/supportsettings/", "/admin/support/dashboard/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

        page = self.client.get(reverse("admin:index"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "/manage/")
        self.assertNotContains(page, "Support")

    def test_application_admin_is_unchanged_by_the_ticket_system(self):
        self.client.force_login(self.superuser)

        page = self.client.get(reverse("admin:applications_application_change", args=[self.oms.pk]))

        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Responsible team")
        self.assertNotContains(page, "support_team")

    def test_no_staff_role_carries_ticket_permissions(self):
        from adminpanel.roles import DEFAULT_ROLES

        for role in DEFAULT_ROLES.values():
            self.assertFalse([permission for permission in role["permissions"] if permission.startswith("support.")])


# ---- Administration (/manage/) -----------------------------------------------------

MANAGE_URLS = [
    ("support:manage-tickets", []),
    ("support:manage-departments", []),
    ("support:manage-department-create", []),
    ("support:manage-applications", []),
    ("support:manage-application-create", []),
    ("support:manage-teams", []),
    ("support:manage-team-create", []),
    ("support:manage-people", []),
    ("support:manage-reports", []),
    ("support:manage-activity", []),
    ("support:manage-settings", []),
]


class ManageAccessTests(SupportTestCase):

    def detail_urls(self):
        return [
            reverse("support:manage-department", args=[self.it.pk]),
            reverse("support:manage-application", args=[self.oms.pk]),
            reverse("support:manage-team", args=[self.oms_team.pk]),
        ]

    def all_urls(self):
        return [reverse(name, args=args) for name, args in MANAGE_URLS] + self.detail_urls()

    def test_administrators_and_superusers_get_in(self):
        for who in (self.admin, self.superuser):
            self.client.force_login(who)

            for url in self.all_urls():
                with self.subTest(who=who.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, 200)

    def test_everyone_else_is_refused_whatever_the_url(self):
        posts = [
            (reverse("support:manage-bulk"), {"action": "close", "tickets": [], "confirm": "yes"}),
            (reverse("support:manage-department-toggle", args=[self.it.pk]), {}),
            (reverse("support:manage-team-member-add", args=[self.oms_team.pk]), {"person": "bob@jivo.in", "role": "manager"}),
            (reverse("support:manage-administrators"), {"person": "bob@jivo.in"}),
            (reverse("support:manage-settings"), {"max_attachment_mb": 500}),
        ]

        for who in (self.alice, self.meera, self.ravi):
            self.client.force_login(who)

            for url in self.all_urls():
                with self.subTest(who=who.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)

            for url, data in posts:
                with self.subTest(who=who.email, post=url):
                    self.assertEqual(self.client.post(url, data).status_code, 403)

        self.assertFalse(TeamMembership.objects.filter(user=self.bob).exists())
        self.assertFalse(SupportAdministrator.objects.filter(user=self.bob).exists())
        self.assertTrue(Department.objects.get(pk=self.it.pk).is_active)
        self.assertEqual(SupportSettings.load().max_attachment_mb, 10)

        self.client.logout()
        response = self.client.get(reverse("support:manage-tickets"))
        self.assertRedirects(response, f"{reverse('support:login')}?next={reverse('support:manage-tickets')}", fetch_redirect_response=False)

    def test_staff_status_alone_grants_nothing(self):
        staff_user = user("staff@jivo.in", is_staff=True)
        self.client.force_login(staff_user)

        self.assertEqual(self.client.get(reverse("support:manage-tickets")).status_code, 403)
        self.assertNotIn("admin", self.client.get("/").context["dashboard"])


class ManageTicketsTests(SupportTestCase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse("support:manage-tickets")

    def ids(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)

        return {ticket.pk for ticket in response.context["page"].object_list}

    def test_filters_and_search(self):
        mine = services.claim(self.ticket(subject="Claimed"), self.meera)
        waiting = self.ticket(subject="Waiting one")
        services.request_information(waiting, self.ravi, "?")
        other = self.ticket(application=self.jsap, creator=self.bob, subject="JSAP one")
        unrouted = self.ticket(application=self.orphan, subject="Nobody's")
        late = self.ticket(subject="Late one")
        Ticket.objects.filter(pk=late.pk).update(resolve_due_at=timezone.now() - timedelta(hours=1))

        everything = {mine.pk, waiting.pk, other.pk, unrouted.pk, late.pk}
        self.assertEqual(self.ids(), everything)
        self.assertEqual(self.ids(status="in_progress"), {mine.pk})
        self.assertEqual(self.ids(status="waiting"), {waiting.pk})
        self.assertEqual(self.ids(status="new"), {other.pk, unrouted.pk, late.pk})
        self.assertEqual(self.ids(assignee="none"), {waiting.pk, other.pk, unrouted.pk, late.pk})
        self.assertEqual(self.ids(assignee=str(self.meera.pk)), {mine.pk})
        self.assertEqual(self.ids(team="none"), {unrouted.pk})
        self.assertEqual(self.ids(team=str(self.other_team.pk)), {other.pk})
        self.assertEqual(self.ids(department=str(self.it.pk)), everything)
        self.assertEqual(self.ids(application=str(self.jsap.pk)), {other.pk})
        self.assertEqual(self.ids(priority="medium"), everything)
        self.assertEqual(self.ids(q=mine.reference), {mine.pk})
        self.assertEqual(self.ids(q="jsap one"), {other.pk})
        self.assertEqual(self.ids(sla="overdue"), {late.pk})
        self.assertEqual(self.ids(sla="resolution"), {late.pk})
        self.assertEqual(self.ids(sla="ok"), everything - {late.pk})

        today = timezone.localdate()
        self.assertEqual(self.ids(created_from=str(today), created_to=str(today)), everything)
        self.assertEqual(self.ids(created_to=str(today - timedelta(days=1))), set())
        self.assertEqual(self.ids(updated_from=str(today + timedelta(days=1))), set())

        page = self.client.get(self.url)
        self.assertContains(page, "Claimed")
        self.assertContains(page, 'name="tickets"')
        self.assertContains(page, "More filters")

    def test_export_is_the_filtered_list_as_csv(self):
        ticket = services.claim(self.ticket(subject="Claimed"), self.meera)
        self.ticket(application=self.jsap, subject="Other")

        response = self.client.get(self.url, {"team": self.oms_team.pk, "export": "csv"})

        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        text = b"".join(response.streaming_content).decode()
        self.assertTrue(text.startswith("\ufeff"))
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
        self.assertEqual(rows[0][:4], ["ticket", "subject", "status", "priority"])
        self.assertEqual([row[0] for row in rows[1:]], [ticket.reference])
        self.assertEqual(rows[1][7], "meera@jivo.in")

    def test_bulk_assign_asks_first_and_skips_other_teams(self):
        oms_ticket = self.ticket()
        jsap_ticket = self.ticket(application=self.jsap)
        url = reverse("support:manage-bulk")
        selected = {"action": "assign", "tickets": [oms_ticket.pk, jsap_ticket.pk], "back": self.url}

        page = self.client.post(url, selected)
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "support/manage/confirm_bulk.html")
        self.assertContains(page, oms_ticket.reference)
        self.assertEqual(Ticket.objects.filter(assignee__isnull=False).count(), 0)

        response = self.client.post(url, {**selected, "confirm": "yes", "assignee": self.meera.pk}, follow=True)

        self.assertContains(response, "1 ticket(s) updated")
        self.assertContains(response, "Skipped 1")
        self.assertEqual(Ticket.objects.get(pk=oms_ticket.pk).assignee, self.meera)
        self.assertIsNone(Ticket.objects.get(pk=jsap_ticket.pk).assignee)

    def test_bulk_status_priority_close_reopen_and_team(self):
        first = self.ticket()
        second = self.ticket()
        url = reverse("support:manage-bulk")

        def act(action, **extra):
            return self.client.post(url, {"action": action, "tickets": [first.pk, second.pk], "confirm": "yes", "back": self.url, **extra}, follow=True)

        act("priority", priority="urgent")
        self.assertEqual(set(Ticket.objects.values_list("priority", flat=True)), {"urgent"})

        act("status", status="waiting")
        self.assertEqual(set(Ticket.objects.values_list("status", flat=True)), {"waiting"})

        act("close", note="duplicate")
        self.assertEqual(set(Ticket.objects.values_list("status", flat=True)), {"closed"})

        response = act("reopen")
        self.assertContains(response, "Say why the tickets are reopened")
        self.assertEqual(set(Ticket.objects.values_list("status", flat=True)), {"closed"})

        act("reopen", note="not a duplicate after all")
        self.assertEqual(set(Ticket.objects.values_list("status", flat=True)), {"reopened"})

        act("team", team=self.other_team.pk)
        self.assertEqual(set(Ticket.objects.values_list("team", flat=True)), {self.other_team.pk})

        self.assertEqual(TicketEvent.objects.filter(type=TicketEvent.Type.CLOSED).count(), 2)

    def test_bulk_without_a_selection(self):
        response = self.client.post(reverse("support:manage-bulk"), {"action": "close", "back": self.url}, follow=True)

        self.assertContains(response, "Select at least one ticket first")

    def test_admin_sees_every_action_on_any_ticket(self):
        ticket = self.ticket()

        page = self.client.get(ticket.get_absolute_url())

        for label in ("Claim ticket", "Assign to", "Responsible team", "Resolve ticket", "Add internal note", "All tickets"):
            self.assertContains(page, label)


class ManageStructureTests(SupportTestCase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_departments(self):
        self.ticket()

        page = self.client.get(reverse("support:manage-departments"))
        self.assertContains(page, "IT Department")
        self.assertContains(page, "Jivo OMS")

        response = self.client.post(
            reverse("support:manage-department-create"),
            {"name": "Finance", "description": "Money", "default_team": self.oms_team.pk, "is_active": "on"},
        )
        finance = Department.objects.get(name="Finance")
        self.assertRedirects(response, reverse("support:manage-department", args=[finance.pk]), fetch_redirect_response=False)
        self.assertEqual(finance.default_team, self.oms_team)

        page = self.client.get(reverse("support:manage-department", args=[self.it.pk]))
        self.assertContains(page, "Orphan app")
        self.assertContains(page, f"{reverse('support:manage-application-create')}?department={self.it.pk}")

        response = self.client.post(reverse("support:manage-department-create"), {"name": "Finance"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "already exists", status_code=400)

        self.client.post(reverse("support:manage-department-toggle", args=[finance.pk]))
        finance.refresh_from_db()
        self.assertFalse(finance.is_active)

    def test_inactive_department_leaves_the_ticket_form(self):
        self.client.post(reverse("support:manage-department-toggle", args=[self.it.pk]))
        self.client.force_login(self.alice)

        self.assertContains(self.client.get(reverse("support:ticket-create")), "No applications accept tickets yet")

    def test_applications_routing(self):
        page = self.client.get(reverse("support:manage-applications"), {"routing": "no_team"})
        self.assertEqual([app.slug for app in page.context["applications"]], ["orphan"])

        page = self.client.get(reverse("support:manage-application", args=[self.oms.pk]))
        self.assertContains(page, "Responsible team")

        ticket = self.ticket()
        response = self.client.post(
            reverse("support:manage-application", args=[self.oms.pk]),
            {"department": self.it.pk, "support_team": self.other_team.pk, "tickets_enabled": "on"},
            follow=True,
        )
        self.assertContains(response, "existing tickets keep theirs")
        self.oms.refresh_from_db()
        self.assertEqual(self.oms.support_team, self.other_team)
        ticket.refresh_from_db()
        self.assertEqual(ticket.team, self.oms_team)

        # Turning tickets off hides the application from the form, not from sign-in.
        self.client.post(reverse("support:manage-application", args=[self.jsap.pk]), {"department": self.it.pk, "support_team": self.other_team.pk})
        self.jsap.refresh_from_db()
        self.assertFalse(self.jsap.tickets_enabled)
        self.assertTrue(self.jsap.is_active)
        self.client.force_login(self.alice)
        self.assertNotContains(self.client.get(reverse("support:ticket-create")), ">JSAP<")

    def test_create_an_application_for_tickets_only(self):
        response = self.client.post(
            reverse("support:manage-application-create"),
            {"name": "JI Factory App", "slug": "ji-factory", "department": self.it.pk, "support_team": self.oms_team.pk, "tickets_enabled": "on"},
        )

        self.assertRedirects(response, reverse("support:manage-applications"), fetch_redirect_response=False)
        app = Application.objects.get(slug="ji-factory")
        self.assertEqual(app.department, self.it)
        self.assertIsNone(app.api_key_hash)
        self.assertEqual(app.users.count(), 0)

        response = self.client.post(reverse("support:manage-application-create"), {"name": "Bad", "slug": "Bad Slug!"})
        self.assertEqual(response.status_code, 400)

    def test_teams_members_and_roles(self):
        services.claim(self.ticket(), self.meera)

        page = self.client.get(reverse("support:manage-teams"))
        self.assertContains(page, "OMS Team")
        self.assertContains(page, "Jivo OMS")

        response = self.client.post(reverse("support:manage-team-create"), {"name": "JI Factory Team", "description": "", "is_active": "on"})
        team = Team.objects.get(name="JI Factory Team")
        self.assertRedirects(response, reverse("support:manage-team", args=[team.pk]), fetch_redirect_response=False)

        add = reverse("support:manage-team-member-add", args=[team.pk])
        self.client.post(add, {"person": "BOB@jivo.in", "role": "manager"})
        membership = TeamMembership.objects.get(team=team, user=self.bob)
        self.assertEqual(membership.role, "manager")
        self.assertTrue(access.is_manager(User.objects.get(pk=self.bob.pk), team.pk))

        response = self.client.post(add, {"person": "nobody@jivo.in", "role": "member"}, follow=True)
        self.assertContains(response, "No account has the email nobody@jivo.in")
        response = self.client.post(add, {"person": "bob@jivo.in", "role": "member"}, follow=True)
        self.assertContains(response, "already in JI Factory Team")

        self.client.post(reverse("support:manage-team-member", args=[team.pk, membership.pk]), {"role": "member"})
        membership.refresh_from_db()
        self.assertEqual(membership.role, "member")

        # Two members, one ticket: counts are per ticket, not per member.
        page = self.client.get(reverse("support:manage-team", args=[self.oms_team.pk]))
        self.assertContains(page, "meera@jivo.in")
        self.assertContains(page, "1 active · 0 unassigned · 1 in progress")
        teams = {team.name: team for team in self.client.get(reverse("support:manage-teams")).context["teams"]}
        self.assertEqual((teams["OMS Team"].in_progress_count, teams["OMS Team"].member_count), (1, 2))

    def test_removing_a_member_returns_their_tickets_to_the_queue(self):
        ticket = services.claim(self.ticket(), self.meera)
        membership = TeamMembership.objects.get(team=self.oms_team, user=self.meera)

        response = self.client.post(reverse("support:manage-team-member", args=[self.oms_team.pk, membership.pk]), {"remove": "1"}, follow=True)

        self.assertContains(response, "1 active ticket(s) went back to the queue")
        ticket.refresh_from_db()
        self.assertIsNone(ticket.assignee)
        self.assertEqual(ticket.status, Ticket.Status.OPEN)
        self.assertFalse(access.is_team_member(User.objects.get(pk=self.meera.pk)))

    def test_people_and_administrators(self):
        page = self.client.get(reverse("support:manage-people"))
        self.assertContains(page, "alice@jivo.in")
        self.assertContains(page, "OMS Team · manager")

        page = self.client.get(reverse("support:manage-people"), {"role": "managers"})
        self.assertEqual([person.email for person in page.context["page"].object_list], ["ravi@jivo.in"])

        url = reverse("support:manage-administrators")
        self.client.post(url, {"person": "bob@jivo.in"})
        self.assertTrue(SupportAdministrator.objects.filter(user=self.bob).exists())
        self.assertTrue(access.is_admin(User.objects.get(pk=self.bob.pk)))

        response = self.client.post(url, {"person": "root@jivo.in"}, follow=True)
        self.assertContains(response, "always an administrator")

        response = self.client.post(url, {"revoke": self.admin.pk}, follow=True)
        self.assertContains(response, "can&#x27;t remove your own administrator role")

        self.client.post(url, {"revoke": self.bob.pk})
        self.assertFalse(SupportAdministrator.objects.filter(user=self.bob).exists())

    def test_dashboard_access_is_given_and_taken_in_people_and_roles(self):
        from boards.access import can_view_dashboards

        url = reverse("support:manage-dashboard-access")
        page = self.client.get(reverse("support:manage-people"))
        self.assertContains(page, "Give dashboard access")
        self.assertContains(page, "Can see (superuser)")

        response = self.client.post(url, {"person": "Alice@jivo.in"}, follow=True)
        self.assertContains(response, "can now see the JI dashboards")
        self.assertTrue(can_view_dashboards(User.objects.get(pk=self.alice.pk)))

        page = self.client.get(reverse("support:manage-people"), {"role": "dashboards"})
        self.assertEqual({person.email for person in page.context["page"].object_list}, {"alice@jivo.in", "root@jivo.in"})
        self.assertContains(page, "Remove dashboards")

        # The Dashboard section appears for her.
        self.client.force_login(self.alice)
        titles = [section["title"] for section in self.client.get("/").context["nav_sections"]]
        self.assertIn("Dashboard", titles)
        self.assertEqual(self.client.post(url, {"person": "bob@jivo.in"}).status_code, 403)

        self.client.force_login(self.admin)
        response = self.client.post(url, {"revoke": self.alice.pk}, follow=True)
        self.assertContains(response, "can no longer see the JI dashboards")
        self.assertFalse(can_view_dashboards(User.objects.get(pk=self.alice.pk)))

        # A staff role given in /admin/ keeps access; the page says so.
        from django.contrib.auth.models import Group

        self.bob.groups.add(Group.objects.get(name="Dashboard viewer"))
        page = self.client.get(reverse("support:manage-people"), {"q": "bob"})
        self.assertContains(page, "Can see (staff role)")
        response = self.client.post(url, {"person": "bob@jivo.in"}, follow=True)
        self.assertContains(response, "can already see the JI dashboards")

        response = self.client.post(url, {"person": "nobody@jivo.in"}, follow=True)
        self.assertContains(response, "No account has the email nobody@jivo.in")

    def test_settings(self):
        page = self.client.get(reverse("support:manage-settings"))
        self.assertContains(page, "SLA deadlines")

        data = {field.name: getattr(SupportSettings.load(), field.name) for field in SupportSettings._meta.fields if field.name not in ("id", "updated_at")}
        data["response_hours_urgent"] = 2
        data = {key: ("on" if value is True else value) for key, value in data.items() if value is not False}

        response = self.client.post(reverse("support:manage-settings"), data)

        self.assertRedirects(response, reverse("support:manage-settings"), fetch_redirect_response=False)
        self.assertEqual(SupportSettings.load().response_hours_urgent, 2)
        self.assertEqual(SupportSettings.objects.count(), 1)

        response = self.client.post(reverse("support:manage-settings"), {**data, "max_attachment_mb": "lots"})
        self.assertEqual(response.status_code, 400)

    def test_activity_history(self):
        ticket = services.claim(self.ticket(), self.meera)
        self.ticket(application=self.jsap)

        page = self.client.get(reverse("support:manage-activity"), {"q": ticket.reference})
        self.assertEqual({event.ticket_id for event in page.context["page"].object_list}, {ticket.pk})
        self.assertContains(page, "Assigned")

        page = self.client.get(reverse("support:manage-activity"), {"type": "created"})
        self.assertEqual(page.context["page"].paginator.count, 2)

        page = self.client.get(reverse("support:manage-activity"), {"q": "meera"})
        self.assertTrue(all(event.actor == self.meera for event in page.context["page"].object_list))


class ReportTests(SupportTestCase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse("support:manage-reports")

    def test_report_figures(self):
        resolved = self.ticket(subject="Resolved")
        Ticket.objects.filter(pk=resolved.pk).update(created_at=timezone.now() - timedelta(hours=10))
        resolved.refresh_from_db()
        services.add_comment(resolved, self.ravi, "On it")
        services.resolve(resolved, self.ravi, "done")
        self.ticket(subject="Open")
        old = self.ticket(subject="Old")
        Ticket.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=60))

        page = self.client.get(self.url)

        self.assertEqual(page.status_code, 200)
        report = page.context["report"]
        self.assertEqual(report["summary"]["created"], 2)
        self.assertEqual(report["summary"]["resolved"], 1)
        self.assertEqual(report["summary"]["pending_now"], 2)
        self.assertEqual(report["summary"]["sla"]["met"], 1)
        self.assertEqual(report["summary"]["response"]["count"], 1)
        self.assertIn("h", report["summary"]["response"]["hours"])
        self.assertEqual(report["departments"][0]["total"], 2)
        self.assertEqual({row["label"]: row["count"] for row in report["statuses"]}["Resolved"], 1)
        self.assertIsNotNone(report["chart"])
        self.assertEqual(report["trend"]["columns"][-1]["total"], 2)

        page = self.client.get(self.url, {"start": "2026-01-01", "end": "2026-12-31", "team": self.oms_team.pk})
        self.assertEqual(page.context["report"]["summary"]["created"], 3)
        self.assertTrue(page.context["report"]["chart_capped"])
        self.assertContains(page, "Daily charts need a range of 92 days or less")

        page = self.client.get(self.url, {"application": self.jsap.pk})
        self.assertEqual(page.context["report"]["summary"]["created"], 0)
        self.assertContains(page, "No tickets in this range")

    def test_exports(self):
        ticket = self.ticket()

        response = self.client.get(self.url, {"export": "tickets"})
        rows = list(csv.reader(io.StringIO(b"".join(response.streaming_content).decode().lstrip("\ufeff"))))
        self.assertEqual(rows[1][0], ticket.reference)

        response = self.client.get(self.url, {"export": "summary"})
        text = b"".join(response.streaming_content).decode()
        self.assertIn("Tickets created,1", text)
        self.assertIn("IT Department,1", text)


# ---- Deployment checks ---------------------------------------------------------

class DeployCheckTests(TestCase):

    def test_media_root_must_exist_and_be_writable(self):
        from .checks import media_root_writable

        with tempfile.TemporaryDirectory() as directory:
            with override_settings(MEDIA_ROOT=directory):
                self.assertEqual(media_root_writable(), [])

            missing = f"{directory}/nope"

            with override_settings(MEDIA_ROOT=missing):
                self.assertEqual([error.id for error in media_root_writable()], ["support.E001"])

            os.chmod(directory, 0o500)

            try:
                with override_settings(MEDIA_ROOT=directory):
                    errors = media_root_writable()
            finally:
                os.chmod(directory, 0o700)

            # Root may write anyway; everyone else gets E002.
            expected = [] if os.getuid() == 0 else ["support.E002"]
            self.assertEqual([error.id for error in errors], expected)


# ---- API ---------------------------------------------------------------------

@override_settings(MEDIA_ROOT=MEDIA)
class SupportAPITests(APITestCase):

    def setUp(self):
        cache.clear()
        self.it = Department.objects.create(name="IT Department")
        self.team = Team.objects.create(name="OMS Team")
        self.ravi = user("ravi@jivo.in", first_name="Ravi")
        TeamMembership.objects.create(team=self.team, user=self.ravi)
        self.oms = Application.objects.create(slug="oms", name="Jivo OMS", department=self.it, support_team=self.team)
        Application.objects.create(slug="hidden", name="Hidden", department=self.it, is_active=False)
        self.alice = user("alice@jivo.in")
        self.bob = user("bob@jivo.in")

    def auth(self, who):
        self.client.force_authenticate(who)

    def test_catalog(self):
        self.auth(self.alice)

        response = self.client.get("/api/v1/support/catalog/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [
            {"id": self.it.pk, "name": "IT Department", "description": "", "applications": [{"id": str(self.oms.pk), "slug": "oms", "name": "Jivo OMS"}]},
        ])
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/v1/support/catalog/").status_code, 401)

    def test_create_list_and_detail(self):
        self.auth(self.alice)

        response = self.client.post(
            "/api/v1/support/tickets/",
            {"department": self.it.pk, "application": "oms", "subject": "Blank PDF", "description": "Order 1042"},
        )

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["status"], "open")
        self.assertEqual(body["team"], "OMS Team")
        self.assertEqual(body["application"], "oms")
        self.assertTrue(body["url"].endswith(f"/tickets/{body["id"]}/"))
        self.assertEqual(Notification.objects.filter(user=self.ravi).count(), 1)

        response = self.client.get("/api/v1/support/tickets/")
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["reference"], body["reference"])

        self.auth(self.bob)
        self.assertEqual(self.client.get("/api/v1/support/tickets/").json()["count"], 0)
        self.assertEqual(self.client.get(f"/api/v1/support/tickets/{body['id']}/").status_code, 404)

        self.auth(self.ravi)
        self.assertEqual(self.client.get("/api/v1/support/tickets/").json()["count"], 0)
        self.assertEqual(self.client.get("/api/v1/support/tickets/?scope=team").json()["count"], 1)
        self.assertEqual(self.client.get(f"/api/v1/support/tickets/{body['id']}/").status_code, 200)

    def test_validation(self):
        self.auth(self.alice)
        other = Department.objects.create(name="Finance")

        response = self.client.post(
            "/api/v1/support/tickets/",
            {"department": other.pk, "application": "oms", "subject": "x", "description": "y"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("application", response.json())

        response = self.client.post(
            "/api/v1/support/tickets/",
            {"department": self.it.pk, "application": "hidden", "subject": "x", "description": "y"},
        )
        self.assertEqual(response.status_code, 400)

    def test_comments_and_internal_notes(self):
        ticket = services.create_ticket(created_by=self.alice, department=self.it, application=self.oms, subject="s", description="d")
        services.add_comment(ticket, self.ravi, "secret", internal=True)
        url = f"/api/v1/support/tickets/{ticket.pk}/"

        self.auth(self.alice)
        detail = self.client.get(url).json()
        self.assertEqual([c["body"] for c in detail["comments"]], [])

        response = self.client.post(url + "comments/", {"body": "hello", "is_internal": True})
        self.assertEqual(response.status_code, 403)

        response = self.client.post(url + "comments/", {"body": "hello"})
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["is_internal"])

        self.auth(self.ravi)
        detail = self.client.get(url).json()
        self.assertEqual([c["body"] for c in detail["comments"]], ["secret", "hello"])

        response = self.client.post(url + "comments/", {"body": "note", "is_internal": True})
        self.assertEqual(response.status_code, 201)

        services.close(ticket, self.ravi)
        self.auth(self.alice)
        response = self.client.post(url + "comments/", {"body": "late"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("closed", response.json()["detail"])
