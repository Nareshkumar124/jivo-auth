import hashlib
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from applications.models import Application
from authentication.models import UserSession

from .models import EmailVerificationToken, PasswordResetToken, User


EMAIL = "alice@example.com"
PASSWORD = "Str0ng!Pass"
NEW_PASSWORD = "N3w!Password"


class UserAPITestCase(APITestCase):

    def setUp(self):
        # Throttle counters live in the default cache.
        cache.clear()

        self.user = User.objects.create_user(
            email=EMAIL,
            password=PASSWORD,
            first_name="Alice",
            is_verified=True,
        )

    def login(self, email=EMAIL, password=PASSWORD):
        return self.client.post(
            reverse("login"),
            {
                "email": email,
                "password": password,
            },
        )

    def authenticate(self):
        tokens = self.login().json()

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {tokens['access']}"
        )

        return tokens

    def forgot_password(self, email=EMAIL):
        return self.client.post(
            reverse("forgot-password"),
            {"email": email},
        )

    def emailed_token(self, subject_word):
        message = next(
            message
            for message in reversed(mail.outbox)
            if subject_word in message.subject
        )

        return re.search(
            r"[?&]token=([\w-]{40,})",
            message.body,
        ).group(1)

    def emailed_reset_token(self):
        return self.emailed_token("password")

    def emailed_verification_token(self):
        return self.emailed_token("Verify")


@override_settings(REGISTRATION_ENABLED=True)
class RegisterTests(UserAPITestCase):

    def register(self, **overrides):
        data = {
            "email": "bob@example.com",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "first_name": "Bob",
            "last_name": "Builder",
        }
        data.update(overrides)

        return self.client.post(reverse("register"), data)

    def test_register_creates_user(self):
        response = self.register()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            response.json(),
            {
                "email": "bob@example.com",
                "first_name": "Bob",
                "last_name": "Builder",
            },
        )

        user = User.objects.get(email="bob@example.com")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertTrue(user.password.startswith("argon2"))
        self.assertFalse(user.is_verified)

    def test_login_requires_verified_email(self):
        self.register()

        refused = self.login("bob@example.com")

        self.assertEqual(refused.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(refused.json()["code"], "email_not_verified")

        self.client.post(
            reverse("verify-email"),
            {"token": self.emailed_verification_token()},
        )

        self.assertEqual(
            self.login("bob@example.com").status_code,
            status.HTTP_200_OK,
        )

    def test_unverified_email_error_needs_the_right_password(self):
        self.register()

        response = self.login("bob@example.com", password="Wrong!Pass1")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotEqual(response.json().get("code"), "email_not_verified")

    @override_settings(REQUIRE_VERIFIED_EMAIL=False)
    def test_verification_can_be_optional(self):
        self.register()

        self.assertEqual(
            self.login("bob@example.com").status_code,
            status.HTTP_200_OK,
        )

    def test_register_rejects_password_mismatch(self):
        response = self.register(password_confirm="Different!1")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.json())

    def test_register_rejects_weak_password(self):
        response = self.register(
            password="weakpassword",
            password_confirm="weakpassword",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.json())

    def test_register_rejects_common_password(self):
        # Meets the composition rules but is in Django's common list.
        response = self.register(
            password="Password1!",
            password_confirm="Password1!",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.json())

    def test_register_rejects_duplicate_email_case_insensitively(self):
        response = self.register(email="ALICE@example.com")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.json())

    def test_register_rejects_duplicate_of_mixed_case_email(self):
        # A row saved without going through UserManager.create_user.
        User.objects.create(email="Carol@Example.com")

        response = self.register(email="carol@example.com")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.json())

    def test_register_requires_email(self):
        response = self.register(email="")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.json())

    def test_register_stores_email_lowercased(self):
        self.register(email="Bob@Example.com")

        self.assertTrue(
            User.objects.filter(email="bob@example.com").exists()
        )

    def test_register_emails_verification_link(self):
        self.register()

        (message,) = mail.outbox
        self.assertEqual(message.to, ["bob@example.com"])
        self.assertIn(
            f"{settings.PUBLIC_URL}/verify-email/?token=",
            message.body,
        )

        token = self.emailed_verification_token()
        stored = EmailVerificationToken.objects.get()

        self.assertEqual(
            stored.token_hash,
            hashlib.sha256(token.encode()).hexdigest(),
        )

    def test_register_grants_no_application_access(self):
        Application.objects.create(slug="oms", name="OMS")

        self.register()

        user = User.objects.get(email="bob@example.com")
        self.assertFalse(user.applications.exists())

    @override_settings(REGISTRATION_ENABLED=False)
    def test_register_can_be_disabled(self):
        response = self.register()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["detail"], "Registration is disabled.")
        self.assertFalse(User.objects.filter(email="bob@example.com").exists())

    @override_settings(REGISTRATION_EMAIL_DOMAINS=["jivo.in"])
    def test_register_can_be_limited_to_domains(self):
        refused = self.register(email="bob@example.com")
        allowed = self.register(email="Bob@Jivo.in")

        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("@jivo.in", refused.json()["email"][0])
        self.assertEqual(allowed.status_code, status.HTTP_201_CREATED)

    @override_settings(REGISTRATION_EMAIL_DOMAINS=["jivo.in"])
    def test_register_domain_check_is_not_fooled_by_subdomains(self):
        response = self.register(email="bob@jivo.in.evil.example")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_accepts_email_in_any_case(self):
        self.register(email="Bob@Example.com")
        User.objects.filter(email="bob@example.com").update(is_verified=True)

        for email in ("Bob@Example.com", "bob@example.com", "BOB@EXAMPLE.COM"):
            with self.subTest(email=email):
                response = self.login(email)

                self.assertEqual(response.status_code, status.HTTP_200_OK)


class CurrentUserTests(UserAPITestCase):

    def test_me_requires_authentication(self):
        response = self.client.get(reverse("current-user"))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_returns_current_user(self):
        self.authenticate()

        response = self.client.get(reverse("current-user"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        body = response.json()
        self.assertEqual(body["id"], str(self.user.id))
        self.assertEqual(body["email"], EMAIL)
        self.assertEqual(body["first_name"], "Alice")
        self.assertEqual(body["apps"], [])
        self.assertNotIn("password", body)

    def test_me_lists_active_applications(self):
        Application.objects.create(slug="oms", name="OMS").users.add(self.user)
        Application.objects.create(
            slug="old",
            name="Old",
            is_active=False,
        ).users.add(self.user)
        self.authenticate()

        response = self.client.get(reverse("current-user"))

        self.assertEqual(response.json()["apps"], ["oms"])

    def test_patch_me_updates_profile(self):
        self.authenticate()

        response = self.client.patch(
            reverse("current-user"),
            {
                "first_name": "Alicia",
                "last_name": "Smith",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["first_name"], "Alicia")

        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Alicia")
        self.assertEqual(self.user.last_name, "Smith")

    def test_patch_me_cannot_change_protected_fields(self):
        self.authenticate()

        self.client.patch(
            reverse("current-user"),
            {
                "email": "mallory@example.com",
                "is_active": False,
                "is_verified": False,
            },
        )

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, EMAIL)
        self.assertTrue(self.user.is_active)
        self.assertTrue(self.user.is_verified)

    def test_put_me_is_not_allowed(self):
        self.authenticate()

        response = self.client.put(
            reverse("current-user"),
            {"first_name": "Alicia"},
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )


class ChangePasswordTests(UserAPITestCase):

    def change_password(self, **overrides):
        data = {
            "current_password": PASSWORD,
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
        }
        data.update(overrides)

        return self.client.post(reverse("change-password"), data)

    def test_change_password_requires_authentication(self):
        response = self.change_password()

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_change_password_updates_password(self):
        self.authenticate()

        response = self.change_password()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.login(password=PASSWORD).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.login(password=NEW_PASSWORD).status_code,
            status.HTTP_200_OK,
        )

    def test_change_password_rejects_wrong_current_password(self):
        self.authenticate()

        response = self.change_password(current_password="Wrong!Pass1")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_password", response.json())

    def test_change_password_rejects_mismatch(self):
        self.authenticate()

        response = self.change_password(new_password_confirm="Other!Pass1")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("new_password", response.json())

    def test_change_password_rejects_weak_password(self):
        self.authenticate()

        response = self.change_password(
            new_password="weakpassword",
            new_password_confirm="weakpassword",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("new_password", response.json())

    def test_change_password_rejects_same_password(self):
        self.authenticate()

        response = self.change_password(
            new_password=PASSWORD,
            new_password_confirm=PASSWORD,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("new_password", response.json())

    def test_change_password_revokes_existing_refresh_tokens(self):
        # README Phase 5: change password -> invalidate old sessions/tokens.
        tokens = self.authenticate()

        self.change_password()

        response = self.client.post(
            reverse("refresh"),
            {"refresh": tokens["refresh"]},
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(
            UserSession.objects.filter(
                user=self.user,
                revoked_at__isnull=True,
            ).exists()
        )

    def test_change_password_invalidates_pending_reset_tokens(self):
        self.forgot_password()
        self.authenticate()

        self.change_password()

        self.assertFalse(
            PasswordResetToken.objects.filter(
                used_at__isnull=True,
            ).exists()
        )


@override_settings(RETURN_RESET_TOKEN_IN_RESPONSE=False)
class ForgotPasswordTests(UserAPITestCase):

    def test_forgot_password_emails_hashed_reset_token(self):
        response = self.forgot_password()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("reset_token", response.json())

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [EMAIL])

        raw_token = self.emailed_reset_token()
        reset_token = PasswordResetToken.objects.get(user=self.user)

        self.assertEqual(
            reset_token.token_hash,
            hashlib.sha256(raw_token.encode()).hexdigest(),
        )
        self.assertGreater(reset_token.expires_at, timezone.now())

    def test_forgot_password_emails_link_to_reset_page(self):
        self.forgot_password()

        self.assertIn(
            f"{settings.PUBLIC_URL}/reset-password/?token=",
            mail.outbox[0].body,
        )

    @override_settings(
        PASSWORD_RESET_URL="https://oms.jivo.in/reset?t={token}",
    )
    def test_forgot_password_link_is_configurable(self):
        self.forgot_password()

        self.assertIn("https://oms.jivo.in/reset?t=", mail.outbox[0].body)

    def test_forgot_password_matches_email_case_insensitively(self):
        response = self.forgot_password("ALICE@Example.com")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)

    def test_forgot_password_response_does_not_reveal_account(self):
        known = self.forgot_password(EMAIL)
        unknown = self.forgot_password("nobody@example.com")

        self.assertEqual(unknown.status_code, status.HTTP_200_OK)
        self.assertEqual(known.json(), unknown.json())

    def test_forgot_password_for_unknown_email_creates_nothing(self):
        self.forgot_password("nobody@example.com")

        self.assertFalse(PasswordResetToken.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_forgot_password_ignores_inactive_users(self):
        self.user.is_active = False
        self.user.save()

        response = self.forgot_password()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(PasswordResetToken.objects.exists())

    @override_settings(RETURN_RESET_TOKEN_IN_RESPONSE=True)
    def test_forgot_password_returns_token_in_local_development(self):
        response = self.forgot_password()

        self.assertEqual(
            response.json()["reset_token"],
            self.emailed_reset_token(),
        )


class ResetPasswordTests(UserAPITestCase):

    def request_reset_token(self):
        self.forgot_password()

        return self.emailed_reset_token()

    def reset_password(self, token, **overrides):
        data = {
            "token": token,
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
        }
        data.update(overrides)

        return self.client.post(reverse("reset-password"), data)

    def test_reset_password_updates_password(self):
        token = self.request_reset_token()

        response = self.reset_password(token)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.login(password=PASSWORD).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.login(password=NEW_PASSWORD).status_code,
            status.HTTP_200_OK,
        )
        self.assertIsNotNone(
            PasswordResetToken.objects.get(user=self.user).used_at
        )

    def test_reset_password_revokes_existing_refresh_tokens(self):
        tokens = self.login().json()
        token = self.request_reset_token()

        self.reset_password(token)

        response = self.client.post(
            reverse("refresh"),
            {"refresh": tokens["refresh"]},
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_reset_password_invalidates_other_reset_tokens(self):
        first_token = self.request_reset_token()
        second_token = self.request_reset_token()

        self.reset_password(second_token)

        response = self.reset_password(
            first_token,
            new_password="An0ther!Pass",
            new_password_confirm="An0ther!Pass",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_token_is_single_use(self):
        token = self.request_reset_token()
        self.reset_password(token)

        response = self.reset_password(
            token,
            new_password="An0ther!Pass",
            new_password_confirm="An0ther!Pass",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_password_rejects_unknown_token(self):
        response = self.reset_password("not-a-real-token")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_password_rejects_expired_token(self):
        token = self.request_reset_token()
        PasswordResetToken.objects.update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        response = self.reset_password(token)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_reset_password_rejects_token_of_deactivated_user(self):
        token = self.request_reset_token()
        self.user.is_active = False
        self.user.save()

        response = self.reset_password(token)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_password_rejects_mismatch(self):
        token = self.request_reset_token()

        response = self.reset_password(
            token,
            new_password_confirm="Other!Pass1",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_password_rejects_weak_password(self):
        token = self.request_reset_token()

        response = self.reset_password(
            token,
            new_password="weakpassword",
            new_password_confirm="weakpassword",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("new_password", response.json())


class ScopedThrottleTests(UserAPITestCase):

    def assert_throttled_after(self, limit, send):
        for _ in range(limit):
            self.assertNotEqual(
                send().status_code,
                status.HTTP_429_TOO_MANY_REQUESTS,
            )

        self.assertEqual(
            send().status_code,
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    def test_register_is_rate_limited(self):
        # settings: "register": "5/hour"
        self.assert_throttled_after(
            5,
            lambda: self.client.post(reverse("register"), {}),
        )

    def test_forgot_password_is_rate_limited(self):
        # settings: "forgot_password": "3/min"
        self.assert_throttled_after(
            3,
            lambda: self.forgot_password("nobody@example.com"),
        )

    def test_reset_password_is_rate_limited(self):
        # settings: "reset_password": "5/min"
        self.assert_throttled_after(
            5,
            lambda: self.client.post(reverse("reset-password"), {}),
        )


class VerifyEmailTests(UserAPITestCase):

    def setUp(self):
        super().setUp()

        User.objects.filter(pk=self.user.pk).update(is_verified=False)
        self.user.refresh_from_db()

    def request_verification_token(self):
        from .services import send_email_verification

        send_email_verification(self.user)

        return self.emailed_verification_token()

    def verify(self, token):
        return self.client.post(reverse("verify-email"), {"token": token})

    def test_verify_email_marks_user_verified(self):
        response = self.verify(self.request_verification_token())

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_verified)

    def test_verification_token_is_single_use(self):
        token = self.request_verification_token()
        self.verify(token)

        self.assertEqual(
            self.verify(token).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_verification_rejects_unknown_token(self):
        response = self.verify("not-a-real-token")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json()["detail"], "Invalid verification token.")

    def test_verification_rejects_expired_token(self):
        token = self.request_verification_token()
        EmailVerificationToken.objects.update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        response = self.verify(token)

        self.assertEqual(response.json()["detail"], "Verification token has expired.")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_verified)

    def test_new_verification_email_invalidates_older_links(self):
        first = self.request_verification_token()
        self.request_verification_token()

        self.assertEqual(
            self.verify(first).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def resend(self, email=EMAIL):
        return self.client.post(
            reverse("resend-verification"),
            {"email": email},
        )

    def test_resend_verification_sends_new_link(self):
        response = self.resend("ALICE@example.com")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.verify(self.emailed_verification_token()).status_code,
            status.HTTP_200_OK,
        )

    def test_resend_verification_does_not_reveal_account(self):
        known = self.resend()
        unknown = self.resend("nobody@example.com")

        self.assertEqual(known.json(), unknown.json())
        self.assertEqual(len(mail.outbox), 1)

    def test_resend_verification_ignores_verified_user(self):
        self.user.is_verified = True
        self.user.save()

        response = self.resend()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 0)

    def test_resend_verification_requires_email(self):
        response = self.client.post(reverse("resend-verification"), {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_refresh_refused_once_email_is_unverified(self):
        self.user.is_verified = True
        self.user.save()
        tokens = self.login().json()
        User.objects.filter(pk=self.user.pk).update(is_verified=False)

        response = self.client.post(
            reverse("refresh"),
            {"refresh": tokens["refresh"]},
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.json()["code"], "email_not_verified")


class HostedPageTests(UserAPITestCase):

    def test_reset_password_page_resets_password(self):
        tokens = self.login().json()
        self.forgot_password()
        token = self.emailed_reset_token()

        page = self.client.get(reverse("reset-password-page"), {"token": token})

        self.assertEqual(page.status_code, status.HTTP_200_OK)
        self.assertContains(page, f'value="{token}"')
        self.assertIn("no-cache", page["Cache-Control"])

        response = self.client.post(
            reverse("reset-password-page"),
            {
                "token": token,
                "new_password": NEW_PASSWORD,
                "new_password_confirm": NEW_PASSWORD,
            },
        )

        self.assertContains(response, "Password changed")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertEqual(
            self.client.post(
                reverse("refresh"),
                {"refresh": tokens["refresh"]},
            ).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_reset_password_page_shows_validation_errors(self):
        self.forgot_password()

        response = self.client.post(
            reverse("reset-password-page"),
            {
                "token": self.emailed_reset_token(),
                "new_password": "weakpassword",
                "new_password_confirm": "weakpassword",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertContains(response, "uppercase", status_code=400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_reset_password_page_rejects_bad_token(self):
        response = self.client.post(
            reverse("reset-password-page"),
            {
                "token": "not-a-real-token",
                "new_password": NEW_PASSWORD,
                "new_password_confirm": NEW_PASSWORD,
            },
        )

        self.assertContains(response, "Invalid reset token.", status_code=400)

    def test_reset_password_page_without_token(self):
        response = self.client.get(reverse("reset-password-page"))

        self.assertContains(response, "Link not valid")

    def test_verify_email_page_verifies_on_post_only(self):
        from .services import send_email_verification

        User.objects.filter(pk=self.user.pk).update(is_verified=False)
        send_email_verification(self.user)
        token = self.emailed_verification_token()

        self.client.get(reverse("verify-email-page"), {"token": token})
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_verified)

        response = self.client.post(
            reverse("verify-email-page"),
            {"token": token},
        )

        self.assertContains(response, "Email verified")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_verified)

    def test_pages_require_csrf_token(self):
        client = self.client_class(enforce_csrf_checks=True)

        response = client.post(
            reverse("verify-email-page"),
            {"token": "anything"},
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AdminTests(UserAPITestCase):

    def setUp(self):
        super().setUp()

        self.admin = User.objects.create_superuser(
            email="admin@jivo.in",
            password=PASSWORD,
        )
        self.client.force_login(self.admin)

        self.oms = Application.objects.create(slug="oms", name="OMS")
        self.oms.users.add(self.user)

    def test_admin_pages_render(self):
        session = UserSession.objects.create(user=self.user, refresh_jti="x")

        for url in (
            reverse("admin:users_user_changelist"),
            reverse("admin:users_user_changelist") + "?applications__id__exact="
            + str(self.oms.id),
            reverse("admin:users_user_add"),
            reverse("admin:users_user_change", args=[self.user.id]),
            reverse("admin:applications_application_changelist"),
            reverse("admin:applications_application_change", args=[self.oms.id]),
            reverse("admin:authentication_usersession_changelist"),
            reverse("admin:authentication_usersession_change", args=[session.id]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_admin_creates_user_with_email(self):
        response = self.client.post(
            reverse("admin:users_user_add"),
            {
                "email": "Dave@Jivo.in",
                "usable_password": "true",
                "password1": "An0ther!Pass",
                "password2": "An0ther!Pass",
                "Application_users-TOTAL_FORMS": "0",
                "Application_users-INITIAL_FORMS": "0",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            User.objects.get(email__iexact="dave@jivo.in").check_password(
                "An0ther!Pass"
            )
        )

    def test_log_out_everywhere_action(self):
        self.login()

        self.client.post(
            reverse("admin:users_user_changelist"),
            {
                "action": "log_out_everywhere",
                "_selected_action": [str(self.user.id)],
            },
        )

        self.assertFalse(
            UserSession.objects.filter(
                user=self.user,
                revoked_at__isnull=True,
            ).exists()
        )


class PasswordChangeHookTests(UserAPITestCase):

    def active_sessions(self):
        return UserSession.objects.filter(
            user=self.user,
            revoked_at__isnull=True,
        )

    def test_any_password_change_ends_sessions(self):
        # E.g. `manage.py changepassword`, which never calls the API.
        self.login()
        self.forgot_password()

        user = User.objects.get(pk=self.user.pk)
        user.set_password(NEW_PASSWORD)
        user.save()

        self.assertFalse(self.active_sessions().exists())
        self.assertFalse(
            PasswordResetToken.objects.filter(used_at__isnull=True).exists()
        )

    def test_disabling_password_ends_sessions(self):
        self.login()

        user = User.objects.get(pk=self.user.pk)
        user.set_unusable_password()
        user.save()

        self.assertFalse(self.active_sessions().exists())

    def test_other_saves_keep_sessions(self):
        self.login()

        user = User.objects.get(pk=self.user.pk)
        user.first_name = "Alicia"
        user.save()

        self.assertTrue(self.active_sessions().exists())

    def test_hash_upgrade_on_login_keeps_sessions(self):
        # An old PBKDF2 hash is upgraded to Argon2 on the next login.
        User.objects.filter(pk=self.user.pk).update(
            password=make_password(PASSWORD, hasher="pbkdf2_sha256"),
        )

        first = self.login()
        second = self.login()

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertTrue(
            User.objects.get(pk=self.user.pk).password.startswith("argon2")
        )
        self.assertEqual(self.active_sessions().count(), 2)

    def test_admin_password_change_ends_sessions(self):
        self.login()
        admin_user = User.objects.create_superuser(
            email="admin@jivo.in",
            password=PASSWORD,
        )
        self.client.force_login(admin_user)

        response = self.client.post(
            reverse("admin:auth_user_password_change", args=[self.user.pk]),
            {
                "usable_password": "true",
                "password1": NEW_PASSWORD,
                "password2": NEW_PASSWORD,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.active_sessions().exists())
