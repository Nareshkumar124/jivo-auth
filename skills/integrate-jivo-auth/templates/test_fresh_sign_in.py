"""
Tests for fresh_sign_in.py, the step-up "type your password again" check
(decision D9). Copy next to it and `jivo_test_tokens.py`, and adapt the line
marked ADAPT. Jivo Auth's sessions API is stood in for; nothing calls it.
"""

import datetime
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from jivo_auth.exceptions import AuthServiceError, AuthServiceUnavailable

from . import fresh_sign_in as fs
from .jivo_test_tokens import make_access_token, use_test_keys

User = get_user_model()


@use_test_keys
class FreshSignInTests(TestCase):

    def setUp(self):
        # ADAPT: required fields of the user model.
        self.user = User.objects.create_user(username="p", email="p@jivo.in", auth_id=uuid.uuid4())
        self.revoked = []

    def check(self, access, sessions, revoke_error=None):
        def jivo_api(method, path, access):
            if isinstance(sessions, Exception):
                raise sessions
            if method == "GET":
                return sessions
            if revoke_error:
                raise revoke_error
            self.revoked.append(path)
            return {}

        with mock.patch.object(fs, "_jivo_api", side_effect=jivo_api):
            return fs.confirm_fresh_sign_in(self.user, access)

    def session(self, age=5, device=None, active=True):
        created = timezone.now() - datetime.timedelta(seconds=age)
        return {"id": "s1", "device_name": device or fs.CONFIRM_DEVICE_NAME, "is_active": active,
                "created_at": created.isoformat()}

    def token(self, **claims):
        return make_access_token(user_id=self.user.auth_id, **claims)

    def test_a_fresh_confirmation_sign_in_passes_and_is_revoked(self):
        self.check(self.token(), [self.session()])
        self.assertEqual(self.revoked, ["/auth/sessions/s1/revoke/"])

    def test_refusals(self):
        cases = {
            "only an everyday session (a refresh, no password)": (
                self.token(), [self.session(age=3600, device="Some app web")]),
            "older than the window": (self.token(), [self.session(age=fs.FRESH_SIGN_IN_SECONDS + 5)]),
            "already ended": (self.token(), [self.session(active=False)]),
            "someone else's token": (make_access_token(user_id=uuid.uuid4()), [self.session()]),
            "no grant for this app": (self.token(apps=["some-other-app"]), [self.session()]),
            "not a token": ("not.a.token", [self.session()]),
        }
        for name, (access, sessions) in cases.items():
            with self.subTest(name), self.assertRaises(fs.FreshSignInRefused):
                self.check(access, sessions)
        self.assertEqual(self.revoked, [])

    def test_the_same_sign_in_twice_is_refused(self):
        with self.assertRaises(fs.FreshSignInRefused):
            self.check(self.token(), [self.session()], revoke_error=AuthServiceError(404, {}))

    def test_jivo_auth_unreachable_is_a_503(self):
        with self.assertRaises(fs.FreshSignInRefused) as caught:
            self.check(self.token(), AuthServiceUnavailable("timed out"))
        self.assertEqual(caught.exception.status, 503)
