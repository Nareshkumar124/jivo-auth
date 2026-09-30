from datetime import timedelta

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from applications.models import Application, hash_api_key
from audit.models import AuditEvent
from authentication.models import UserSession
from users.models import User

from .admin_tools import describe_user_agent
from .charts import axis, bar_list, column_chart, Series
from .roles import DEFAULT_ROLES


PASSWORD = "Str0ng!Pass"


def staff(email, role=None, **extra):
    user = User.objects.create_user(
        email=email,
        password=PASSWORD,
        is_staff=True,
        is_verified=True,
        **extra,
    )

    if role:
        user.groups.add(Group.objects.get(name=role))

    return user


class AdminTestCase(TestCase):

    def setUp(self):
        cache.clear()

        self.superuser = User.objects.create_superuser(email="root@jivo.in", password=PASSWORD)
        self.alice = User.objects.create_user(email="alice@jivo.in", password=PASSWORD, is_verified=True)
        self.oms = Application.objects.create(slug="oms", name="Jivo OMS")

    def changelist(self, model):
        return reverse(f"admin:{model}_changelist")


class ChartTests(TestCase):

    def test_axis_ticks_are_whole_numbers(self):
        self.assertEqual(axis(0), (1, [0, 1]))
        self.assertEqual(axis(38), (40, [0, 10, 20, 30, 40]))
        self.assertEqual(axis(41), (50, [0, 10, 20, 30, 40, 50]))
        self.assertEqual(axis(99), (100, [0, 20, 40, 60, 80, 100]))

    def test_column_chart(self):
        today = timezone.localdate()
        days = [today - timedelta(days=1), today]

        chart = column_chart(
            days,
            [Series("series-1", "Successful", {today: 30}), Series("series-2", "Failed", {today: 8})],
            today,
        )

        column = chart["columns"][1]
        self.assertEqual(column["total"], 38)
        self.assertEqual(column["height"], 95.0)
        self.assertTrue(column["is_today"])
        self.assertEqual([s["value"] for s in column["segments"]], [30, 8])
        self.assertEqual(chart["columns"][0]["segments"], [])

    def test_bar_list_is_sorted_and_scaled(self):
        bars = bar_list([("A", 5, "/a"), ("B", 20, "/b")])

        self.assertEqual([bar["label"] for bar in bars], ["B", "A"])
        self.assertEqual(bars[0]["width"], 100.0)

    def test_user_agent_descriptions(self):
        self.assertEqual(
            describe_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64) Chrome/130.0 Safari/537.36"),
            "Chrome on Windows",
        )
        self.assertEqual(
            describe_user_agent("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Version/18.0 Safari/604.1"),
            "Safari on iPhone",
        )
        self.assertEqual(describe_user_agent("jivo-auth-client/0.2.0"), "jivo-auth-client/0.2.0")
        self.assertEqual(describe_user_agent(""), "")


class DefaultRoleTests(TestCase):

    def test_roles_exist_with_their_permissions(self):
        for name, role in DEFAULT_ROLES.items():
            with self.subTest(role=name):
                group = Group.objects.get(name=name)
                granted = {
                    f"{p.content_type.app_label}.{p.codename}"
                    for p in group.permissions.select_related("content_type")
                }
                self.assertEqual(granted, set(role["permissions"]))

    def test_no_role_can_manage_staff_or_roles(self):
        for role in DEFAULT_ROLES.values():
            for permission in role["permissions"]:
                self.assertNotIn(permission, {"users.add_user", "adminpanel.change_staffrole", "auth.change_group"})


class DashboardTests(AdminTestCase):

    def test_superuser_sees_every_section(self):
        self.oms.users.add(self.alice)
        AuditEvent.objects.create(type=AuditEvent.Type.LOGIN_FAILED, email="x@jivo.in", ip_address="198.51.100.9")
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        for text in ("Sign-ins", "New users", "Users per application", "Recent activity", "Failed sign-ins by IP", "Add user"):
            self.assertContains(response, text)

    def test_counts_are_not_inflated_by_multiple_grants(self):
        ecom = Application.objects.create(slug="ecom", name="Ecom")
        self.oms.users.add(self.alice)
        ecom.users.add(self.alice)
        self.client.force_login(self.superuser)

        dashboard = self.client.get(reverse("admin:index")).context["dashboard"]
        users_tile = next(tile for tile in dashboard["tiles"] if tile["label"] == "Users")

        self.assertEqual(users_tile["value"], User.objects.count())

    def test_failed_signins_by_ip_needs_three_failures(self):
        for _ in range(3):
            AuditEvent.objects.create(type=AuditEvent.Type.LOGIN_FAILED, email="a@jivo.in", ip_address="198.51.100.9")
        AuditEvent.objects.create(type=AuditEvent.Type.LOGIN_FAILED, email="b@jivo.in", ip_address="198.51.100.10")
        self.client.force_login(self.superuser)

        dashboard = self.client.get(reverse("admin:index")).context["dashboard"]

        self.assertEqual([row["ip_address"] for row in dashboard["failed_ips"]], ["198.51.100.9"])

    def test_sections_follow_permissions(self):
        auditor = staff("auditor@jivo.in", role="Auditor")
        self.client.force_login(auditor)

        response = self.client.get(reverse("admin:index"))

        self.assertContains(response, "Sign-ins")
        self.assertNotContains(response, "Add user")
        self.assertNotContains(response, "Register application")

    def test_staff_without_permissions_sees_a_hint(self):
        self.client.force_login(staff("nobody@jivo.in"))

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.context["dashboard"]["tiles"], [])
        self.assertContains(response, "Ask a superuser for a staff role")

    def test_sidebar_only_lists_permitted_sections(self):
        self.client.force_login(staff("support@jivo.in", role="Support"))

        sections = self.client.get(reverse("admin:index")).context["nav_sections"]
        labels = [item["label"] for section in sections for item in section["items"]]

        self.assertEqual(labels, ["Users", "Applications", "Sessions", "Audit log"])


class UserAdminPermissionTests(AdminTestCase):

    def setUp(self):
        super().setUp()

        self.support = staff("support@jivo.in", role="Support")

    def test_support_cannot_edit_a_superuser(self):
        self.client.force_login(self.support)

        response = self.client.post(
            reverse("admin:users_user_change", args=[self.superuser.pk]),
            {"email": "root@jivo.in", "is_active": ""},
        )

        self.assertEqual(response.status_code, 403)
        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.is_active)

    def test_support_cannot_reset_a_superusers_password(self):
        self.client.force_login(self.support)

        response = self.client.post(
            reverse("admin:users_user_tool", args=[self.superuser.pk, "password-reset"]),
        )

        self.assertEqual(response.status_code, 403)

    def test_support_cannot_grant_staff_or_roles(self):
        self.client.force_login(self.support)

        page = self.client.get(reverse("admin:users_user_change", args=[self.alice.pk]))

        self.assertIn("is_staff", page.context["adminform"].readonly_fields)
        self.assertIn("groups", page.context["adminform"].readonly_fields)
        self.assertNotIn("user_permissions", str(page.context["adminform"].form.fields))

    def test_support_cannot_edit_roles(self):
        self.client.force_login(self.support)

        response = self.client.get(reverse("admin:adminpanel_staffrole_changelist"))

        self.assertEqual(response.status_code, 403)

    def test_saving_without_access_permission_keeps_grants(self):
        self.oms.users.add(self.alice)
        self.client.force_login(self.support)

        data = {
            "email": "alice@jivo.in",
            "first_name": "Alicia",
            "last_name": "",
            "is_active": "on",
            "is_verified": "on",
        }
        response = self.client.post(reverse("admin:users_user_change", args=[self.alice.pk]), data)

        self.assertEqual(response.status_code, 302)
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.first_name, "Alicia")
        self.assertTrue(self.alice.applications.filter(pk=self.oms.pk).exists())

    def test_superuser_edits_access_with_checkboxes(self):
        ecom = Application.objects.create(slug="ecom", name="Ecom")
        self.oms.users.add(self.alice)
        self.client.force_login(self.superuser)

        response = self.client.post(
            reverse("admin:users_user_change", args=[self.alice.pk]),
            {
                "email": "alice@jivo.in",
                "first_name": "",
                "last_name": "",
                "is_active": "on",
                "is_verified": "on",
                "applications": [str(ecom.pk)],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.alice.applications.values_list("slug", flat=True)), ["ecom"])
        self.assertEqual(AuditEvent.objects.filter(type=AuditEvent.Type.ACCESS_REVOKED).count(), 1)


class UserActionTests(AdminTestCase):

    def run_action(self, action, users, confirm=True, **extra):
        data = {
            "action": action,
            "_selected_action": [str(user.pk) for user in users],
            **extra,
        }

        if confirm:
            data["jv_confirm"] = "yes"

        return self.client.post(self.changelist("users_user"), data)

    def test_deactivate_asks_first(self):
        self.client.force_login(self.superuser)

        page = self.run_action("deactivate", [self.alice], confirm=False)

        self.assertContains(page, "Deactivate users")
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.is_active)

    def test_deactivate_ends_sessions_and_skips_self(self):
        UserSession.objects.create(user=self.alice, refresh_jti="jti-1")
        self.client.force_login(self.superuser)

        self.run_action("deactivate", [self.alice, self.superuser])

        self.alice.refresh_from_db()
        self.superuser.refresh_from_db()
        self.assertFalse(self.alice.is_active)
        self.assertTrue(self.superuser.is_active)
        self.assertIsNotNone(UserSession.objects.get(user=self.alice).revoked_at)

    def test_support_actions_skip_superusers(self):
        self.client.force_login(staff("support@jivo.in", role="Support"))

        self.run_action("deactivate", [self.alice, self.superuser])

        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.is_active)

    def test_grant_and_remove_access(self):
        self.client.force_login(self.superuser)

        self.run_action("grant_access", [self.alice], application=str(self.oms.pk))
        self.assertTrue(self.alice.applications.filter(pk=self.oms.pk).exists())

        self.run_action("revoke_access", [self.alice], application=str(self.oms.pk))
        self.assertFalse(self.alice.applications.exists())

    def test_access_actions_need_application_permission(self):
        self.client.force_login(staff("support@jivo.in", role="Support"))

        actions = self.client.get(self.changelist("users_user")).context["action_form"].fields["action"].choices
        names = {name for name, _ in actions}

        self.assertIn("deactivate", names)
        self.assertNotIn("grant_access", names)

    def test_mark_verified(self):
        User.objects.filter(pk=self.alice.pk).update(is_verified=False)
        self.client.force_login(self.superuser)

        self.run_action("mark_verified", [self.alice], confirm=False)

        self.alice.refresh_from_db()
        self.assertTrue(self.alice.is_verified)

    def test_list_filters_render(self):
        self.client.force_login(self.superuser)

        for query in ("?has_access=no", "?joined=7d", "?seen=never", "?is_verified__exact=0", "?q=alice"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(self.changelist("users_user") + query).status_code, 200)

    def test_empty_search_shows_empty_state(self):
        self.client.force_login(self.superuser)

        response = self.client.get(self.changelist("users_user") + "?q=nobody-at-all")

        self.assertContains(response, "No users match")


class ApplicationAdminTests(AdminTestCase):

    def test_rotate_key_shows_it_once(self):
        self.client.force_login(self.superuser)

        response = self.client.post(reverse("admin:applications_application_rotate_key", args=[self.oms.pk]))

        self.oms.refresh_from_db()
        raw_key = response.context["api_key"]

        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.oms.api_key_hash, hash_api_key(raw_key))
        self.assertContains(response, raw_key)

    def test_rotate_key_needs_post_and_permission(self):
        self.client.force_login(self.superuser)
        self.assertEqual(
            self.client.get(reverse("admin:applications_application_rotate_key", args=[self.oms.pk])).status_code,
            405,
        )

        self.client.force_login(staff("support@jivo.in", role="Support"))
        self.assertEqual(
            self.client.post(reverse("admin:applications_application_rotate_key", args=[self.oms.pk])).status_code,
            403,
        )

    def test_slug_is_locked_after_creation(self):
        self.client.force_login(self.superuser)

        page = self.client.get(reverse("admin:applications_application_change", args=[self.oms.pk]))

        self.assertIn("slug", page.context["adminform"].readonly_fields)
        self.assertContains(page, "Create API key")


class SessionAdminTests(AdminTestCase):

    def test_revoke_asks_first_and_records(self):
        session = UserSession.objects.create(user=self.alice, refresh_jti="jti-1")
        self.client.force_login(self.superuser)
        url = self.changelist("authentication_usersession")
        data = {"action": "revoke", "_selected_action": [str(session.pk)]}

        self.assertContains(self.client.post(url, data), "Revoke sessions")

        self.client.post(url, {**data, "jv_confirm": "yes"})

        session.refresh_from_db()
        self.assertIsNotNone(session.revoked_at)
        self.assertEqual(
            AuditEvent.objects.get(type=AuditEvent.Type.SESSIONS_REVOKED).details,
            {"count": 1},
        )

    def test_status_filter(self):
        UserSession.objects.create(user=self.alice, refresh_jti="live")
        UserSession.objects.create(user=self.alice, refresh_jti="gone", revoked_at=timezone.now())
        self.client.force_login(self.superuser)

        response = self.client.get(self.changelist("authentication_usersession") + "?status=revoked")

        self.assertEqual(response.context["cl"].result_count, 1)


class PageTests(AdminTestCase):

    def test_login_page_is_branded(self):
        response = self.client.get(reverse("admin:login"))

        self.assertContains(response, "adminpanel/css/admin.css")
        self.assertContains(response, "Jivo Auth")

    def test_password_widget_shows_a_status(self):
        self.client.force_login(self.superuser)

        page = self.client.get(reverse("admin:users_user_change", args=[self.alice.pk]))

        self.assertContains(page, "Password set")
        self.assertNotContains(page, "salt")

    def test_token_tables_are_hidden(self):
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:index"))

        self.assertNotContains(response, "token_blacklist")
