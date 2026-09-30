from datetime import timedelta
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from applications.models import Application
from authentication.models import UserSession
from users.models import User

from .models import AuditEvent


PASSWORD = "Str0ng!Pass"
NEW_PASSWORD = "N3w!Password"


class AuditTestCase(APITestCase):

    def setUp(self):
        cache.clear()

        self.user = User.objects.create_user(
            email="alice@jivo.in",
            password=PASSWORD,
            is_verified=True,
        )

    def events(self, type_):
        return AuditEvent.objects.filter(type=type_)

    def login(self, password=PASSWORD, **extra):
        return self.client.post(
            reverse("login"),
            {"email": "alice@jivo.in", "password": password},
            **extra,
        )


class SignInEventTests(AuditTestCase):

    def test_successful_login_is_recorded_with_client_details(self):
        self.login(REMOTE_ADDR="203.0.113.5", HTTP_USER_AGENT="Browser/1.0")

        event = self.events(AuditEvent.Type.LOGIN_SUCCEEDED).get()

        self.assertEqual(event.user, self.user)
        self.assertEqual(event.email, "alice@jivo.in")
        self.assertEqual(event.ip_address, "203.0.113.5")
        self.assertEqual(event.user_agent, "Browser/1.0")
        self.assertEqual(event.details["session"], str(UserSession.objects.get().pk))
        self.assertIsNone(event.actor)

    def test_wrong_password_is_recorded_against_the_account(self):
        self.login(password="Wrong!Pass1")

        event = self.events(AuditEvent.Type.LOGIN_FAILED).get()

        self.assertEqual(event.user, self.user)
        self.assertEqual(event.details["reason"], "invalid_credentials")
        self.assertEqual(event.summary, "wrong email or password")
        self.assertEqual(event.severity, AuditEvent.Severity.WARNING)

    def test_unknown_email_is_recorded_without_a_user(self):
        self.client.post(
            reverse("login"),
            {"email": "Nobody@Jivo.in", "password": PASSWORD},
        )

        event = self.events(AuditEvent.Type.LOGIN_FAILED).get()

        self.assertIsNone(event.user)
        self.assertEqual(event.email, "nobody@jivo.in")

    def test_unverified_login_records_its_reason(self):
        User.objects.filter(pk=self.user.pk).update(is_verified=False)

        self.login()

        event = self.events(AuditEvent.Type.LOGIN_FAILED).get()
        self.assertEqual(event.details["reason"], "email_not_verified")

    def test_logout_is_recorded(self):
        refresh = self.login().json()["refresh"]

        self.client.post(reverse("logout"), {"refresh": refresh})

        self.assertEqual(self.events(AuditEvent.Type.LOGOUT).get().user, self.user)

    def test_token_reuse_is_a_critical_event(self):
        tokens = self.login().json()
        self.client.post(reverse("refresh"), {"refresh": tokens["refresh"]})
        UserSession.objects.update(rotated_at=timezone.now() - timedelta(minutes=5))

        self.client.post(reverse("refresh"), {"refresh": tokens["refresh"]})

        event = self.events(AuditEvent.Type.TOKEN_REUSED).get()
        self.assertEqual(event.user, self.user)
        self.assertEqual(event.severity, AuditEvent.Severity.CRITICAL)

    def test_revoking_all_sessions_is_recorded(self):
        access = self.login().json()["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        self.client.post(reverse("revoke-all-sessions"))

        event = self.events(AuditEvent.Type.SESSIONS_REVOKED).get()
        self.assertEqual(event.details, {"count": 1, "scope": "all"})


class AccountEventTests(AuditTestCase):

    def test_password_change_is_recorded(self):
        self.user.set_password(NEW_PASSWORD)
        self.user.save()

        event = self.events(AuditEvent.Type.PASSWORD_CHANGED).get()
        self.assertEqual(event.user, self.user)

    def test_password_reset_is_recorded_as_a_reset(self):
        self.client.post(reverse("forgot-password"), {"email": "alice@jivo.in"})
        self.assertTrue(self.events(AuditEvent.Type.PASSWORD_RESET_REQUESTED).exists())

        from users.services import reset_password
        from users.models import PasswordResetToken
        from users.services import _create_token, PASSWORD_RESET_TOKEN_LIFETIME

        raw = _create_token(PasswordResetToken, self.user, PASSWORD_RESET_TOKEN_LIFETIME)
        reset_password(raw, NEW_PASSWORD)

        self.assertTrue(self.events(AuditEvent.Type.PASSWORD_RESET).exists())
        self.assertFalse(self.events(AuditEvent.Type.PASSWORD_CHANGED).exists())

    def test_account_creation_records_its_source(self):
        self.assertEqual(
            self.events(AuditEvent.Type.ACCOUNT_CREATED).get(user=self.user).details,
            {"source": "system"},
        )

        with self.settings(REGISTRATION_ENABLED=True):
            self.client.post(
                reverse("register"),
                {
                    "email": "bob@jivo.in",
                    "password": PASSWORD,
                    "password_confirm": PASSWORD,
                },
            )

        bob = User.objects.get(email="bob@jivo.in")
        self.assertEqual(
            self.events(AuditEvent.Type.ACCOUNT_CREATED).get(user=bob).details,
            {"source": "registration"},
        )

    def test_deactivation_and_reactivation_are_recorded(self):
        user = User.objects.get(pk=self.user.pk)
        user.is_active = False
        user.save()
        user.is_active = True
        user.save()

        self.assertTrue(self.events(AuditEvent.Type.ACCOUNT_DEACTIVATED).exists())
        self.assertTrue(self.events(AuditEvent.Type.ACCOUNT_REACTIVATED).exists())

    def test_saving_without_status_change_records_nothing(self):
        user = User.objects.get(pk=self.user.pk)
        user.first_name = "Alicia"
        user.save()

        self.assertFalse(
            AuditEvent.objects.exclude(type=AuditEvent.Type.ACCOUNT_CREATED).exists()
        )

    def test_email_verification_is_recorded(self):
        from users.services import send_email_verification, verify_email
        from users.models import EmailVerificationToken
        from users.services import _create_token, EMAIL_VERIFICATION_TOKEN_LIFETIME

        send_email_verification(self.user)
        raw = _create_token(EmailVerificationToken, self.user, EMAIL_VERIFICATION_TOKEN_LIFETIME)
        verify_email(raw)

        self.assertTrue(self.events(AuditEvent.Type.EMAIL_VERIFIED).exists())


class AccessEventTests(AuditTestCase):

    def setUp(self):
        super().setUp()

        self.oms = Application.objects.create(slug="oms", name="OMS")
        self.ecom = Application.objects.create(slug="ecom", name="Ecom")

    def test_granting_from_the_application_side(self):
        self.oms.users.add(self.user)

        event = self.events(AuditEvent.Type.ACCESS_GRANTED).get()
        self.assertEqual((event.user, event.application), (self.user, self.oms))

    def test_changes_from_the_user_side_record_only_the_difference(self):
        self.user.applications.set([self.oms])
        self.user.applications.set([self.ecom])

        granted = self.events(AuditEvent.Type.ACCESS_GRANTED)
        revoked = self.events(AuditEvent.Type.ACCESS_REVOKED)

        self.assertEqual(
            sorted(event.application.slug for event in granted),
            ["ecom", "oms"],
        )
        self.assertEqual([event.application.slug for event in revoked], ["oms"])

    def test_clear_records_each_removed_grant(self):
        self.user.applications.set([self.oms, self.ecom])

        self.user.applications.clear()

        self.assertEqual(self.events(AuditEvent.Type.ACCESS_REVOKED).count(), 2)

    def test_api_key_rotation_is_recorded(self):
        self.oms.set_new_api_key()

        event = self.events(AuditEvent.Type.API_KEY_ROTATED).get()
        self.assertEqual(event.application, self.oms)
        self.assertEqual(event.details["key_prefix"], self.oms.api_key_prefix)


class PruneTests(AuditTestCase):

    def test_prune_deletes_only_old_events(self):
        old = AuditEvent.objects.create(type=AuditEvent.Type.LOGOUT)
        AuditEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=400))
        recent = AuditEvent.objects.create(type=AuditEvent.Type.LOGOUT)

        stdout = StringIO()
        call_command("prune_audit_events", "--older-than-days", "365", stdout=stdout)

        self.assertFalse(AuditEvent.objects.filter(pk=old.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(pk=recent.pk).exists())
        self.assertIn("Deleted 1", stdout.getvalue())


class AuditAdminTests(AuditTestCase):

    def setUp(self):
        super().setUp()

        self.admin = User.objects.create_superuser(email="admin@jivo.in", password=PASSWORD)
        self.client.force_login(self.admin)

    def test_log_is_read_only(self):
        event = AuditEvent.objects.first()

        self.assertEqual(
            self.client.get(reverse("admin:audit_auditevent_add")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

        page = self.client.get(reverse("admin:audit_auditevent_change", args=[event.pk]))
        self.assertEqual(page.status_code, status.HTTP_200_OK)
        self.assertNotContains(page, 'name="_save"')

    def test_filters_and_search_render(self):
        for query in ("?severity=warning", "?when=7d", "?type__exact=login_failed", "?q=alice"):
            with self.subTest(query=query):
                response = self.client.get(reverse("admin:audit_auditevent_changelist") + query)
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_severity_filter(self):
        self.login(password="Wrong!Pass1")

        response = self.client.get(reverse("admin:audit_auditevent_changelist") + "?severity=warning")

        types = {event.type for event in response.context["cl"].result_list}
        self.assertEqual(types, {AuditEvent.Type.LOGIN_FAILED})

    def test_admin_actions_record_the_actor(self):
        oms = Application.objects.create(slug="oms", name="OMS")

        self.client.post(
            reverse("admin:users_user_changelist"),
            {
                "action": "grant_access",
                "_selected_action": [str(self.user.pk)],
                "application": str(oms.pk),
                "jv_confirm": "yes",
            },
            REMOTE_ADDR="10.0.0.9",
        )

        event = self.events(AuditEvent.Type.ACCESS_GRANTED).get()
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.ip_address, "10.0.0.9")

    def test_csv_export(self):
        self.login(password="Wrong!Pass1")

        response = self.client.post(
            reverse("admin:audit_auditevent_changelist"),
            {
                "action": "export_csv",
                "_selected_action": [str(pk) for pk in AuditEvent.objects.values_list("pk", flat=True)],
            },
        )

        body = b"".join(response.streaming_content).decode()

        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertTrue(body.startswith("time,event,severity,account,by,"))
        self.assertIn("login_failed,warning,alice@jivo.in", body)
