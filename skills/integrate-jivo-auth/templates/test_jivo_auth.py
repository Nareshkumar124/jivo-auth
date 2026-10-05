"""
Tests for the switch (phase 4): requests signed in with Jivo Auth resolve to
the app's own user rows, and nothing else gets in.

Copy as `tests_jivo_auth.py` next to `jivo_test_tokens.py` (set APP and URL
there) and adapt the lines marked ADAPT. Tokens are real RS256 tokens signed
with a throwaway key; Jivo Auth is never called (AuthClient is mocked).

What the switch must guarantee, one test each:
- a linked user's token is the SAME row (same pk), with their records;
- no app grant -> 403; expired, foreign-signed, refresh-type -> 401;
- a locally deactivated user -> 401;
- first sign-in links the one unlinked row with that email (D4), else
  creates a new row, and never claims a row that is already linked;
- the old sign-in URL is gone (410 or 404);
- the admin: no local password works, no password-setting page, and (if
  that's the choice made) no add form.
"""

import time
import uuid
from unittest import mock, skipUnless

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from jivo_auth.exceptions import AuthServiceError

from .jivo_test_tokens import APP, URL, make_access_token, use_test_keys

User = get_user_model()
LINK_BY_EMAIL = bool(getattr(settings, "JIVO_AUTH", {}).get("LOCAL_USER_LINK_BY_EMAIL"))

# ADAPT: an endpoint that returns the signed-in user and its primary key as
# `id` somewhere in the body (the app's "me"/profile endpoint).
ME_URL = "/api/me/"
# ADAPT: the old sign-in URL and what it answers now (410 stub, or 404).
OLD_LOGIN_URL, OLD_LOGIN_STATUS = "/api/auth/login/", 410
# ADAPT: the user model's admin URL prefix (app_label/model).
ADMIN_USER_URL = "/admin/users/user/"


def make_user(username, email, **kwargs):
    # ADAPT: required fields of the user model.
    return User.objects.create_user(username=username, password="CorrectHorse9!", email=email, **kwargs)


def me_id(response):
    # ADAPT: where ME_URL's body carries the primary key.
    body = response.json()
    return body.get("id", body.get("data", {}).get("id"))


def bearer(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


@use_test_keys
class JivoTokenTests(TestCase):

    def setUp(self):
        self.user = make_user("priya", "priya@jivo.in", auth_id=uuid.uuid4())

    def token(self, **claims):
        return make_access_token(user_id=self.user.auth_id, email=self.user.email, **claims)

    def test_a_linked_users_token_is_that_same_row(self):
        response = bearer(self.token()).get(ME_URL)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(me_id(response), self.user.pk)

    def test_no_app_grant_is_403(self):
        self.assertEqual(bearer(self.token(apps=["some-other-app"])).get(ME_URL).status_code, 403)

    def test_an_expired_token_is_401(self):
        now = int(time.time())
        self.assertEqual(bearer(self.token(iat=now - 3600, exp=now - 600)).get(ME_URL).status_code, 401)

    def test_a_token_signed_by_another_key_is_401(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        forged = jwt.encode({"token_type": "access", "sub": str(self.user.auth_id), "email": self.user.email,
                             "apps": [APP], "iss": URL, "iat": now, "exp": now + 900}, key, algorithm="RS256")
        self.assertEqual(bearer(forged).get(ME_URL).status_code, 401)

    def test_a_refresh_token_is_not_an_access_token(self):
        self.assertEqual(bearer(self.token(token_type="refresh")).get(ME_URL).status_code, 401)

    def test_a_locally_deactivated_user_is_401(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(bearer(self.token()).get(ME_URL).status_code, 401)

    @skipUnless(LINK_BY_EMAIL, "decision D4 is off")
    def test_first_sign_in_links_the_unlinked_row_with_that_email(self):
        old = make_user("old-timer", "Old.Timer@jivo.in")
        sub = uuid.uuid4()
        response = bearer(make_access_token(user_id=sub, email="old.timer@jivo.in")).get(ME_URL)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(me_id(response), old.pk)
        old.refresh_from_db()
        self.assertEqual(old.auth_id, sub)
        self.assertEqual(old.email, "old.timer@jivo.in")  # Jivo's lowercased form

    def test_first_sign_in_without_a_matching_row_creates_one(self):
        before = User.objects.count()
        response = bearer(make_access_token(email="new.person@jivo.in")).get(ME_URL)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(User.objects.count(), before + 1)
        self.assertFalse(User.objects.get(pk=me_id(response)).has_usable_password())

    def test_a_linked_row_is_never_claimed_by_email(self):
        # ADAPT: with `email = EmailField(unique=True)`, the package's new row
        # collides with this one (IntegrityError). Then assert the answer the
        # app gives instead, or drop the request and keep the last two lines.
        original = self.user.auth_id
        response = bearer(make_access_token(email=self.user.email)).get(ME_URL)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotEqual(me_id(response), self.user.pk)
        self.user.refresh_from_db()
        self.assertEqual(self.user.auth_id, original)

    def test_the_old_sign_in_is_gone(self):
        response = APIClient().post(OLD_LOGIN_URL, {"username": "priya", "password": "CorrectHorse9!"},
                                    format="json")
        self.assertEqual(response.status_code, OLD_LOGIN_STATUS)


# The admin renders {% static %}; manifest storages (whitenoise and Django's
# own) need a collectstatic run that tests don't do.
@override_settings(STORAGES={
    **getattr(settings, "STORAGES", {}),  # Django 4.2+
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class AdminSignInTests(TestCase):

    def setUp(self):
        self.boss = make_user("boss", "boss@jivo.in", auth_id=uuid.uuid4(), is_staff=True, is_superuser=True)

    def test_only_jivo_auth_checks_passwords(self):
        self.assertNotIn("django.contrib.auth.backends.ModelBackend", settings.AUTHENTICATION_BACKENDS)

    def test_an_old_local_password_does_not_open_the_admin(self):
        with mock.patch("jivo_auth.backends.AuthClient") as client:
            client.return_value.login.side_effect = AuthServiceError(401, {"detail": "No active account."})
            self.client.post("/admin/login/", {"username": "boss@jivo.in", "password": "CorrectHorse9!",
                                               "next": "/admin/"})
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_admin_cannot_set_a_password(self):
        """A 404 (user_change_password overridden) or a redirect (plain
        ModelAdmin) are both fine: no password form, and nothing changes."""
        self.client.force_login(self.boss, backend="jivo_auth.backends.JivoAuthBackend")
        url = f"{ADMIN_USER_URL}{self.boss.pk}/password/"
        self.client.post(url, {"password1": "Attacker9Pass!", "password2": "Attacker9Pass!"})
        response = self.client.get(url, follow=True)
        self.assertIn(response.status_code, (200, 404))
        self.assertNotIn(b'name="password1"', response.content)
        self.boss.refresh_from_db()
        self.assertFalse(self.boss.check_password("Attacker9Pass!"))

    # ADAPT: drop this if the admin has an "add a Jivo user" form instead.
    def test_users_are_not_added_in_the_admin(self):
        self.client.force_login(self.boss, backend="jivo_auth.backends.JivoAuthBackend")
        self.assertEqual(self.client.get(f"{ADMIN_USER_URL}add/").status_code, 403)
