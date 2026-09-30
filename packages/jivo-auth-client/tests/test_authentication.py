import os
import time
import uuid
from unittest import mock

import jwt
from django.conf import settings
from django.contrib.auth.models import User
from django.core.checks import run_checks
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from jivo_auth.users import JivoUser

from .fake_auth import fake_auth_service, new_rsa_key, public_pem


def jivo_auth(**overrides):
    return override_settings(
        JIVO_AUTH={
            **settings.JIVO_AUTH,
            **overrides,
        },
    )


class ClientTestCase(TestCase):

    def setUp(self):
        context = fake_auth_service()
        self.service = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

        self.alice = self.service.add_user("alice@jivo.in")
        self.api = APIClient()

    def get(self, token, path="/api/whoami/"):
        return self.api.get(path, HTTP_AUTHORIZATION=f"Bearer {token}")


class JivoJWTAuthenticationTests(ClientTestCase):

    def test_accepts_valid_token(self):
        response = self.get(self.service.access_token(self.alice))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "id": self.alice["id"],
                "email": "alice@jivo.in",
                "type": "JivoUser",
            },
        )

    def test_without_token_is_401(self):
        response = self.api.get("/api/whoami/")

        self.assertEqual(response.status_code, 401)
        self.assertIn("WWW-Authenticate", response)

    def test_ignores_other_authorization_schemes(self):
        response = self.api.get(
            "/api/whoami/",
            HTTP_AUTHORIZATION="Basic YWxpY2U6c2VjcmV0",
        )

        # Not authenticated by this class, so the view sees no user.
        self.assertEqual(response.status_code, 401)

    def test_rejects_malformed_header(self):
        response = self.api.get(
            "/api/whoami/",
            HTTP_AUTHORIZATION="Bearer one two",
        )

        self.assertEqual(response.status_code, 401)

    def test_rejects_garbage_token(self):
        self.assertEqual(self.get("not-a-jwt").status_code, 401)

    def test_rejects_expired_token(self):
        token = self.service.access_token(
            self.alice,
            exp=int(time.time()) - 60,
        )

        response = self.get(token)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Token has expired.")

    def test_tolerates_small_clock_differences(self):
        now = int(time.time())

        token = self.service.access_token(
            self.alice,
            iat=now + 5,
            exp=now - 5,
        )

        self.assertEqual(self.get(token).status_code, 200)

    def test_rejects_refresh_token(self):
        token = self.service.access_token(self.alice, token_type="refresh")

        self.assertEqual(self.get(token).status_code, 401)

    def test_rejects_other_issuer(self):
        token = self.service.access_token(self.alice, iss="https://evil.test")

        self.assertEqual(self.get(token).status_code, 401)

    def test_rejects_token_without_issuer(self):
        token = self.service.access_token(self.alice, iss=None)

        self.assertEqual(self.get(token).status_code, 401)

    def test_rejects_token_signed_with_another_key(self):
        token = self.service.access_token(self.alice, key=new_rsa_key())

        self.assertEqual(self.get(token).status_code, 401)

    def test_rejects_hs256_token_signed_with_the_public_key(self):
        # Algorithm confusion: only RS256 is accepted.
        self.get(self.service.access_token(self.alice))
        now = int(time.time())

        token = jwt.encode(
            {
                "token_type": "access",
                "sub": self.alice["id"],
                "apps": ["oms"],
                "iss": "https://auth.example.test",
                "iat": now,
                "exp": now + 60,
            },
            "some-secret-that-is-at-least-32-bytes-long",
            algorithm="HS256",
            headers={"kid": self.service.kid},
        )

        self.assertEqual(self.get(token).status_code, 401)

    def test_rejects_token_without_app_access(self):
        bob = self.service.add_user("bob@jivo.in", apps=["ecom"])

        response = self.get(self.service.access_token(bob))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"],
            "Your account does not have access to this application.",
        )

    @jivo_auth(APP=None)
    def test_without_app_setting_every_user_is_refused(self):
        response = self.get(self.service.access_token(self.alice))

        self.assertEqual(response.status_code, 403)

    @jivo_auth(APP=None, ALLOW_ALL_USERS=True)
    def test_allow_all_users_accepts_any_user(self):
        bob = self.service.add_user("bob@jivo.in", apps=[])

        self.assertEqual(self.get(self.service.access_token(bob)).status_code, 200)

    def test_is_admin_user_permission_denies_instead_of_crashing(self):
        response = self.get(
            self.service.access_token(self.alice),
            path="/api/admin-only/",
        )

        self.assertEqual(response.status_code, 403)

    def test_jivo_user_from_claims(self):
        user = JivoUser.from_claims(
            {
                "sub": self.alice["id"],
                "email": "alice@jivo.in",
                "apps": ["oms"],
            }
        )

        self.assertEqual(user.pk, uuid.UUID(self.alice["id"]))
        self.assertTrue(user.has_app("oms"))
        self.assertFalse(user.has_perm("anything"))
        self.assertTrue(user.is_authenticated)


class KeyTests(ClientTestCase):

    def test_signing_keys_are_fetched_once(self):
        for _ in range(3):
            self.assertEqual(
                self.get(self.service.access_token(self.alice)).status_code,
                200,
            )

        self.assertEqual(len(self.service.calls_to("/.well-known/jwks.json")), 1)

    def test_tokens_verify_while_auth_service_is_down(self):
        self.get(self.service.access_token(self.alice))

        self.service.down = True

        self.assertEqual(
            self.get(self.service.access_token(self.alice)).status_code,
            200,
        )

    def test_unknown_key_ids_refetch_at_most_once_a_minute(self):
        self.get(self.service.access_token(self.alice))

        for _ in range(3):
            token = self.service.access_token(self.alice, kid="unknown")
            self.assertEqual(self.get(token).status_code, 401)

        self.assertEqual(len(self.service.calls_to("/.well-known/jwks.json")), 1)

    def test_rotated_key_is_picked_up(self):
        self.get(self.service.access_token(self.alice))

        self.service.private_key = new_rsa_key()
        self.service.kid = "test-key-2"

        # The first fetch was over a minute ago.
        with mock.patch(
            "jivo_auth.keys.time.monotonic",
            return_value=time.monotonic() + 61,
        ):
            response = self.get(self.service.access_token(self.alice))

        self.assertEqual(response.status_code, 200)

    def test_public_key_setting_verifies_without_network(self):
        self.service.down = True

        with jivo_auth(PUBLIC_KEY=public_pem(self.service.private_key)):
            response = self.get(self.service.access_token(self.alice))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls, [])

    def test_hs256_secret(self):
        secret = "a-shared-secret-that-is-at-least-32-bytes"
        now = int(time.time())

        token = jwt.encode(
            {
                "token_type": "access",
                "sub": self.alice["id"],
                "apps": ["oms"],
                "iss": "https://auth.example.test",
                "iat": now,
                "exp": now + 60,
            },
            secret,
            algorithm="HS256",
        )

        with jivo_auth(SECRET=secret):
            self.assertEqual(self.get(token).status_code, 200)

        with jivo_auth(SECRET="another-secret-that-is-at-least-32-bytes"):
            self.assertEqual(self.get(token).status_code, 401)


@jivo_auth(LOCAL_USERS=True)
class LocalUserTests(ClientTestCase):

    def test_creates_local_user_on_first_request(self):
        response = self.get(self.service.access_token(self.alice))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["type"], "User")

        user = User.objects.get(username=self.alice["id"])
        self.assertEqual(user.email, "alice@jivo.in")
        self.assertFalse(user.has_usable_password())

    def test_reuses_local_user_and_follows_email(self):
        self.get(self.service.access_token(self.alice))

        self.alice["email"] = "alice.smith@jivo.in"
        self.get(self.service.access_token(self.alice))

        user = User.objects.get()
        self.assertEqual(user.email, "alice.smith@jivo.in")

    def test_rejects_deactivated_local_user(self):
        self.get(self.service.access_token(self.alice))
        User.objects.update(is_active=False)

        self.assertEqual(
            self.get(self.service.access_token(self.alice)).status_code,
            401,
        )

    def test_local_staff_passes_is_admin_user(self):
        self.get(self.service.access_token(self.alice))
        User.objects.update(is_staff=True)

        response = self.get(
            self.service.access_token(self.alice),
            path="/api/admin-only/",
        )

        self.assertEqual(response.status_code, 200)


class SettingsTests(TestCase):

    def check_ids(self):
        return {
            message.id
            for message in run_checks()
            if message.id.startswith("jivo_auth.")
        }

    def test_valid_settings_pass_checks(self):
        self.assertEqual(self.check_ids(), set())

    @jivo_auth(APPS="oms")
    def test_unknown_setting_is_an_error(self):
        self.assertIn("jivo_auth.E001", self.check_ids())

    @override_settings(JIVO_AUTH={"APP": "oms"})
    def test_missing_key_source_is_an_error(self):
        self.assertIn("jivo_auth.E002", self.check_ids())

    @jivo_auth(APP=None)
    def test_missing_app_is_an_error(self):
        self.assertIn("jivo_auth.E003", self.check_ids())

    @jivo_auth(APP=None, ALLOW_ALL_USERS=True)
    def test_allow_all_users_is_a_warning(self):
        ids = self.check_ids()

        self.assertIn("jivo_auth.W001", ids)
        self.assertNotIn("jivo_auth.E003", ids)

    @jivo_auth(API_KEY=None)
    def test_session_login_without_api_key_is_a_warning(self):
        self.assertIn("jivo_auth.W002", self.check_ids())

    @override_settings(JIVO_AUTH={})
    def test_settings_fall_back_to_environment(self):
        with mock.patch.dict(
            os.environ,
            {
                "JIVO_AUTH_URL": "https://auth.jivo.in/",
                "JIVO_AUTH_APP": "oms",
                "JIVO_AUTH_API_KEY": "jivo_from-env",
                "JIVO_AUTH_LEEWAY": "30",
                "JIVO_AUTH_NUM_PROXIES": "1",
            },
        ):
            from jivo_auth import settings as conf

            self.assertEqual(conf.auth_url(), "https://auth.jivo.in")
            self.assertEqual(conf.issuer(), "https://auth.jivo.in")
            self.assertEqual(
                conf.jwks_url(),
                "https://auth.jivo.in/.well-known/jwks.json",
            )
            self.assertEqual(conf.get("LEEWAY"), 30.0)
            self.assertEqual(conf.get("NUM_PROXIES"), 1)
            self.assertEqual(self.check_ids(), set())
