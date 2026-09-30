import time

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings

from jivo_auth.client import AuthClient
from jivo_auth.session import SESSION_KEY, refresh_tokens

from .fake_auth import API_KEY, PASSWORD, fake_auth_service


class SessionTestCase(TestCase):

    def setUp(self):
        cache.clear()

        context = fake_auth_service()
        self.service = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

        self.alice = self.service.add_user("alice@jivo.in")

    def log_in(self, email="alice@jivo.in", password=PASSWORD):
        return self.client.post(
            "/login/",
            {"username": email, "password": password},
            HTTP_USER_AGENT="Browser/1.0",
            REMOTE_ADDR="203.0.113.9",
        )

    def session_tokens(self):
        return self.client.session.get(SESSION_KEY)

    def expire_access_token(self):
        session = self.client.session
        session[SESSION_KEY]["access_exp"] = int(time.time()) - 1
        session.save()


class LoginTests(SessionTestCase):

    def test_login_creates_local_user_and_stores_tokens(self):
        response = self.log_in()

        self.assertRedirects(
            response,
            "/whoami/",
            fetch_redirect_response=False,
        )

        user = User.objects.get(username=self.alice["id"])
        self.assertEqual(user.email, "alice@jivo.in")
        self.assertFalse(user.has_usable_password())

        tokens = self.session_tokens()
        self.assertIn(tokens["refresh"], self.service.sessions)

        self.assertEqual(
            self.client.get("/whoami/").json(),
            {
                "username": self.alice["id"],
                "email": "alice@jivo.in",
            },
        )

    def test_login_forwards_app_key_and_end_user_details(self):
        self.log_in()

        call = self.service.calls_to("/api/v1/auth/login/")[0]

        self.assertEqual(call["headers"]["x-jivo-app-key"], API_KEY)
        self.assertEqual(call["headers"]["x-jivo-client-ip"], "203.0.113.9")
        self.assertEqual(call["headers"]["user-agent"], "Browser/1.0")
        self.assertEqual(call["data"]["device_name"], "oms")

    def test_login_ignores_client_supplied_forwarded_for(self):
        self.client.post(
            "/login/",
            {"username": "alice@jivo.in", "password": PASSWORD},
            REMOTE_ADDR="198.51.100.7",
            HTTP_X_FORWARDED_FOR="203.0.113.99",
        )

        call = self.service.calls_to("/api/v1/auth/login/")[0]
        self.assertEqual(call["headers"]["x-jivo-client-ip"], "198.51.100.7")

    def test_login_uses_ip_added_by_trusted_proxy(self):
        with override_settings(
            JIVO_AUTH={**settings.JIVO_AUTH, "NUM_PROXIES": 1},
        ):
            self.client.post(
                "/login/",
                {"username": "alice@jivo.in", "password": PASSWORD},
                REMOTE_ADDR="10.0.0.2",
                # What Nginx's $proxy_add_x_forwarded_for produces.
                HTTP_X_FORWARDED_FOR="203.0.113.99, 198.51.100.7",
            )

        call = self.service.calls_to("/api/v1/auth/login/")[0]
        self.assertEqual(call["headers"]["x-jivo-client-ip"], "198.51.100.7")

    def test_wrong_password_fails(self):
        response = self.log_in(password="Wrong!Pass1")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())
        self.assertIsNone(self.session_tokens())

    def test_user_without_app_access_is_refused_and_session_ended(self):
        self.service.add_user("bob@jivo.in", apps=["ecom"])

        response = self.log_in("bob@jivo.in")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())
        self.assertEqual(self.service.sessions, {})

    def test_locally_deactivated_user_is_refused(self):
        self.log_in()
        self.client.post("/logout/")
        User.objects.update(is_active=False)

        response = self.log_in()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.sessions, {})

    def test_auth_service_down_fails_login_cleanly(self):
        self.service.down = True

        response = self.log_in()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())


class MiddlewareTests(SessionTestCase):

    def setUp(self):
        super().setUp()

        self.log_in()
        self.first_tokens = self.session_tokens()

    def test_fresh_tokens_are_not_refreshed(self):
        self.client.get("/whoami/")

        self.assertEqual(self.service.calls_to("/api/v1/auth/refresh/"), [])

    def test_expiring_tokens_are_refreshed(self):
        self.expire_access_token()

        response = self.client.get("/whoami/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.service.calls_to("/api/v1/auth/refresh/")), 1)
        self.assertNotEqual(
            self.session_tokens()["refresh"],
            self.first_tokens["refresh"],
        )

    def test_revoked_session_logs_user_out(self):
        # E.g. the user changed their password or was logged out everywhere.
        self.service.sessions.clear()
        self.expire_access_token()

        response = self.client.get("/whoami/")

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.service.calls_to("/api/v1/auth/logout/"), [])

    def test_deactivated_user_is_logged_out(self):
        self.alice["active"] = False
        self.expire_access_token()

        self.assertEqual(self.client.get("/whoami/").status_code, 302)

    def test_losing_app_access_logs_user_out(self):
        self.alice["apps"] = []
        self.expire_access_token()

        response = self.client.get("/whoami/")

        self.assertEqual(response.status_code, 302)
        # The session the refresh created is ended too.
        self.assertEqual(self.service.sessions, {})

    def test_auth_service_down_keeps_user_logged_in(self):
        self.service.down = True
        self.expire_access_token()

        self.assertEqual(self.client.get("/whoami/").status_code, 200)

    def test_logout_ends_auth_service_session(self):
        self.client.post("/logout/")

        self.assertNotIn(self.first_tokens["refresh"], self.service.sessions)
        self.assertEqual(len(self.service.calls_to("/api/v1/auth/logout/")), 1)

    def test_users_without_jivo_tokens_are_left_alone(self):
        self.client.logout()
        local_admin = User.objects.create_user("admin", password="x")
        self.client.force_login(
            local_admin,
            backend="django.contrib.auth.backends.ModelBackend",
        )

        self.assertEqual(self.client.get("/whoami/").status_code, 200)
        self.assertEqual(self.service.calls_to("/api/v1/auth/refresh/"), [])


class RefreshTokensTests(SessionTestCase):

    def test_concurrent_refreshes_share_one_result(self):
        tokens = self.service.tokens(self.alice)

        first = refresh_tokens(tokens["refresh"])
        second = refresh_tokens(tokens["refresh"])

        self.assertEqual(first, second)
        self.assertEqual(len(self.service.calls_to("/api/v1/auth/refresh/")), 1)


class AuthClientTests(SessionTestCase):

    def test_get_users_sends_api_key_and_ids(self):
        bob = self.service.add_user("bob@jivo.in")

        users = AuthClient().get_users([bob["id"]])

        self.assertEqual([user["email"] for user in users], ["bob@jivo.in"])

        call = self.service.calls_to("/api/v1/apps/users/")[0]
        self.assertEqual(call["headers"]["x-jivo-app-key"], API_KEY)
        self.assertEqual(call["query"]["id"], [bob["id"]])

    def test_get_users_batches_large_id_lists(self):
        ids = [self.alice["id"]] * 250

        AuthClient().get_users(ids)

        self.assertEqual(
            [
                len(call["query"]["id"])
                for call in self.service.calls_to("/api/v1/apps/users/")
            ],
            [100, 100, 50],
        )

    def test_get_users_without_ids_lists_everyone_with_access(self):
        self.service.add_user("bob@jivo.in")
        self.service.add_user("carol@jivo.in", apps=["ecom"])

        emails = {user["email"] for user in AuthClient().get_users()}

        self.assertEqual(emails, {"alice@jivo.in", "bob@jivo.in"})
