from datetime import timedelta
from unittest import mock

import jwt
from django.conf import settings
from django.core.cache import cache
from django.test import LiveServerTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from jivo_auth.authentication import JivoJWTAuthentication
from jivo_auth.client import AuthClient
from jivo_auth.exceptions import AuthServiceError
from jivo_auth.keys import clear_jwks_caches
from jivo_auth.tokens import decode_access_token
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.test import APIRequestFactory, APITestCase
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from applications.models import Application
from users.models import User

from .models import UserSession


EMAIL = "alice@example.com"
PASSWORD = "Str0ng!Pass"


def behind_proxies(count):
    return override_settings(
        REST_FRAMEWORK={
            **settings.REST_FRAMEWORK,
            "NUM_PROXIES": count,
        },
    )


class AuthAPITestCase(APITestCase):

    def setUp(self):
        # Throttle counters live in the default cache.
        cache.clear()

        # Return 500 responses instead of re-raising, so tests can assert
        # on the status code a real client would see.
        self.client.raise_request_exception = False

        self.user = User.objects.create_user(
            email=EMAIL,
            password=PASSWORD,
            is_verified=True,
        )

    def login(self, email=EMAIL, password=PASSWORD, **extra):
        return self.client.post(
            reverse("login"),
            {
                "email": email,
                "password": password,
                "device_name": "Test Laptop",
            },
            **extra,
        )

    def tokens(self, email=EMAIL):
        return self.login(email).json()

    def refresh(self, refresh_token):
        return self.client.post(
            reverse("refresh"),
            {"refresh": refresh_token},
        )

    def authenticate(self, access_token):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {access_token}"
        )


class LoginTests(AuthAPITestCase):

    def test_login_returns_tokens_and_creates_session(self):
        response = self.login(
            HTTP_USER_AGENT="test-agent/1.0",
            REMOTE_ADDR="203.0.113.5",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        body = response.json()
        access = AccessToken(body["access"])
        refresh = RefreshToken(body["refresh"])

        self.assertEqual(access["sub"], str(self.user.id))
        self.assertEqual(access["token_type"], "access")

        session = UserSession.objects.get(user=self.user)
        self.assertEqual(session.refresh_jti, refresh["jti"])
        self.assertEqual(session.device_name, "Test Laptop")
        self.assertEqual(session.user_agent, "test-agent/1.0")
        self.assertEqual(session.ip_address, "203.0.113.5")
        self.assertTrue(session.is_active)

    def test_tokens_carry_claims_for_other_applications(self):
        Application.objects.create(slug="oms", name="OMS").users.add(self.user)
        Application.objects.create(slug="ecom", name="Ecom").users.add(self.user)
        Application.objects.create(slug="hr", name="HR")

        body = self.login().json()

        for token in (AccessToken(body["access"]), RefreshToken(body["refresh"])):
            with self.subTest(token_type=token["token_type"]):
                self.assertEqual(token["email"], EMAIL)
                self.assertEqual(token["apps"], ["ecom", "oms"])
                self.assertEqual(token["iss"], settings.SIMPLE_JWT["ISSUER"])

    def test_tokens_leave_out_inactive_applications(self):
        Application.objects.create(
            slug="oms",
            name="OMS",
            is_active=False,
        ).users.add(self.user)

        self.assertEqual(AccessToken(self.tokens()["access"])["apps"], [])

    def test_tokens_name_their_signing_key(self):
        header = jwt.get_unverified_header(self.tokens()["access"])

        self.assertEqual(header["alg"], "RS256")
        self.assertEqual(header["kid"], settings.JWT_PUBLIC_JWK["kid"])

    def test_login_ignores_forwarded_for_without_trusted_proxy(self):
        self.login(
            REMOTE_ADDR="203.0.113.5",
            HTTP_X_FORWARDED_FOR="198.51.100.7",
        )

        session = UserSession.objects.get(user=self.user)
        self.assertEqual(session.ip_address, "203.0.113.5")

    def test_login_uses_ip_added_by_trusted_proxy(self):
        with behind_proxies(1):
            self.login(
                REMOTE_ADDR="10.0.0.2",
                HTTP_X_FORWARDED_FOR="198.51.100.7, 203.0.113.5",
            )

        session = UserSession.objects.get(user=self.user)
        self.assertEqual(session.ip_address, "203.0.113.5")

    def test_login_tolerates_malformed_forwarded_for(self):
        with behind_proxies(1):
            response = self.login(HTTP_X_FORWARDED_FOR="not-an-ip")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(
            UserSession.objects.get(user=self.user).ip_address
        )

    def test_login_rejects_overlong_device_name(self):
        response = self.client.post(
            reverse("login"),
            {
                "email": EMAIL,
                "password": PASSWORD,
                "device_name": "x" * 256,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("device_name", response.json())
        self.assertFalse(UserSession.objects.exists())

    def test_login_without_device_name(self):
        response = self.client.post(
            reverse("login"),
            {
                "email": EMAIL,
                "password": PASSWORD,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            UserSession.objects.get(user=self.user).device_name,
            "",
        )

    def test_login_rejects_wrong_password(self):
        response = self.login(password="Wrong!Pass1")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(UserSession.objects.exists())

    def test_login_rejects_unknown_email(self):
        response = self.login(email="nobody@example.com")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_rejects_inactive_user(self):
        self.user.is_active = False
        self.user.save()

        response = self.login()

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_is_rate_limited(self):
        # settings: "login": "5/min"
        for _ in range(5):
            self.login(password="Wrong!Pass1")

        response = self.login(password="Wrong!Pass1")

        self.assertEqual(
            response.status_code,
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    def test_login_rate_limit_ignores_spoofed_forwarded_for(self):
        for attempt in range(5):
            self.login(
                password="Wrong!Pass1",
                HTTP_X_FORWARDED_FOR=f"198.51.100.{attempt}",
            )

        response = self.login(
            password="Wrong!Pass1",
            HTTP_X_FORWARDED_FOR="198.51.100.99",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_429_TOO_MANY_REQUESTS,
        )


    def test_login_limit_is_per_account(self):
        User.objects.create_user(
            email="bob@example.com",
            password=PASSWORD,
            is_verified=True,
        )

        for _ in range(5):
            self.login(password="Wrong!Pass1")

        response = self.login(email="bob@example.com")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_login_limit_per_ip(self):
        # settings: "login_ip": "30/min"
        for attempt in range(30):
            self.login(email=f"user{attempt}@example.com")

        response = self.login(email="someone-else@example.com")

        self.assertEqual(
            response.status_code,
            status.HTTP_429_TOO_MANY_REQUESTS,
        )


class AccessTokenTests(AuthAPITestCase):

    def test_access_token_authenticates_requests(self):
        self.authenticate(self.tokens()["access"])

        response = self.client.get(reverse("current-user"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_refresh_token_cannot_be_used_as_access_token(self):
        self.authenticate(self.tokens()["refresh"])

        response = self.client.get(reverse("current-user"))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class RefreshTests(AuthAPITestCase):

    def test_refresh_rotates_tokens_and_updates_session(self):
        tokens = self.tokens()
        session = UserSession.objects.get(user=self.user)

        response = self.refresh(tokens["refresh"])

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        body = response.json()
        self.assertIn("access", body)
        self.assertNotEqual(body["refresh"], tokens["refresh"])

        session.refresh_from_db()
        self.assertEqual(
            session.refresh_jti,
            RefreshToken(body["refresh"])["jti"],
        )

    def test_refresh_updates_claims(self):
        tokens = self.tokens()
        Application.objects.create(slug="oms", name="OMS").users.add(self.user)

        body = self.refresh(tokens["refresh"]).json()

        self.assertEqual(AccessToken(body["access"])["apps"], ["oms"])
        self.assertEqual(RefreshToken(body["refresh"])["apps"], ["oms"])

    def test_refresh_drops_revoked_application_access(self):
        oms = Application.objects.create(slug="oms", name="OMS")
        oms.users.add(self.user)
        tokens = self.tokens()
        oms.users.remove(self.user)

        body = self.refresh(tokens["refresh"]).json()

        self.assertEqual(AccessToken(body["access"])["apps"], [])

    def test_refresh_requires_token(self):
        response = self.client.post(reverse("refresh"), {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_refresh_rejects_malformed_token(self):
        response = self.refresh("not-a-jwt")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_rejects_access_token(self):
        response = self.refresh(self.tokens()["access"])

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_rejects_deactivated_user(self):
        tokens = self.tokens()
        self.user.is_active = False
        self.user.save()

        response = self.refresh(tokens["refresh"])

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_rejects_token_without_session(self):
        response = self.refresh(str(RefreshToken.for_user(self.user)))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def end_grace_period(self):
        UserSession.objects.update(
            rotated_at=timezone.now()
            - settings.REFRESH_TOKEN_REUSE_GRACE
            - timedelta(seconds=1),
        )

    def test_rotated_token_is_accepted_again_within_grace_period(self):
        tokens = self.tokens()
        first = self.refresh(tokens["refresh"]).json()

        again = self.refresh(tokens["refresh"])

        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertEqual(again.json()["refresh"], first["refresh"])
        self.assertEqual(
            AccessToken(again.json()["access"])["apps"],
            AccessToken(first["access"])["apps"],
        )

        # The rotated token still works as the session's current one.
        self.assertEqual(
            self.refresh(first["refresh"]).status_code,
            status.HTTP_200_OK,
        )

    def test_refresh_rejects_rotated_token_after_grace_period(self):
        tokens = self.tokens()
        self.refresh(tokens["refresh"])
        self.end_grace_period()

        response = self.refresh(tokens["refresh"])

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_reuse_after_grace_period_revokes_session(self):
        tokens = self.tokens()
        current = self.refresh(tokens["refresh"]).json()
        self.end_grace_period()

        self.refresh(tokens["refresh"])

        self.assertFalse(UserSession.objects.get().is_active)
        self.assertEqual(
            self.refresh(current["refresh"]).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_older_rotated_tokens_are_rejected(self):
        tokens = self.tokens()
        second = self.refresh(tokens["refresh"]).json()
        self.refresh(second["refresh"])

        response = self.refresh(tokens["refresh"])

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_rejects_token_blacklisted_elsewhere(self):
        tokens = self.tokens()
        RefreshToken(tokens["refresh"]).blacklist()

        response = self.refresh(tokens["refresh"])

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_stores_issued_token(self):
        body = self.refresh(self.tokens()["refresh"]).json()
        jti = RefreshToken(body["refresh"])["jti"]

        self.assertEqual(
            OutstandingToken.objects.get(jti=jti).token,
            body["refresh"],
        )


class VerifyTests(AuthAPITestCase):

    def verify(self, token):
        return self.client.post(
            reverse("verify"),
            {"token": token},
        )

    def test_verify_accepts_valid_token(self):
        response = self.verify(self.tokens()["access"])

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_verify_rejects_malformed_token(self):
        response = self.verify("not-a-jwt")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_verify_rejects_blacklisted_token(self):
        tokens = self.tokens()
        self.refresh(tokens["refresh"])

        response = self.verify(tokens["refresh"])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class LogoutTests(AuthAPITestCase):

    def logout(self, refresh_token):
        return self.client.post(
            reverse("logout"),
            {"refresh": refresh_token},
        )

    def test_logout_blacklists_token_and_revokes_session(self):
        tokens = self.tokens()

        response = self.logout(tokens["refresh"])

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        jti = RefreshToken(tokens["refresh"], verify=False)["jti"]
        self.assertTrue(
            BlacklistedToken.objects.filter(token__jti=jti).exists()
        )
        self.assertFalse(
            UserSession.objects.get(refresh_jti=jti).is_active
        )

    def test_logout_ignores_expired_or_invalid_access_token(self):
        tokens = self.tokens()
        self.authenticate("not-a-valid-access-token")

        response = self.logout(tokens["refresh"])

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_logged_out_token_cannot_be_refreshed(self):
        tokens = self.tokens()
        self.logout(tokens["refresh"])

        response = self.refresh(tokens["refresh"])

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_only_ends_that_session(self):
        laptop = self.tokens()
        phone = self.tokens()

        self.logout(laptop["refresh"])

        self.assertEqual(
            self.refresh(phone["refresh"]).status_code,
            status.HTTP_200_OK,
        )

    def test_logout_twice_is_rejected(self):
        tokens = self.tokens()
        self.logout(tokens["refresh"])

        response = self.logout(tokens["refresh"])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_logout_requires_refresh_token(self):
        response = self.client.post(reverse("logout"), {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_logout_rejects_invalid_refresh_token(self):
        response = self.logout("not-a-jwt")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_logout_rejects_access_token(self):
        response = self.logout(self.tokens()["access"])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class JWKSTests(AuthAPITestCase):

    def test_jwks_is_public_and_cacheable(self):
        response = self.client.get(reverse("jwks"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("max-age", response["Cache-Control"])

    def test_jwks_key_verifies_issued_tokens(self):
        access = self.tokens()["access"]
        keys = self.client.get(reverse("jwks")).json()["keys"]

        key_set = jwt.PyJWKSet.from_dict({"keys": keys})
        kid = jwt.get_unverified_header(access)["kid"]
        key = next(key for key in key_set.keys if key.key_id == kid)

        claims = jwt.decode(
            access,
            key.key,
            algorithms=["RS256"],
            issuer=settings.SIMPLE_JWT["ISSUER"],
        )

        self.assertEqual(claims["sub"], str(self.user.id))

    def test_jwks_contains_no_private_key_material(self):
        (key,) = self.client.get(reverse("jwks")).json()["keys"]

        self.assertEqual(set(key), {"kty", "n", "e", "kid", "use", "alg"})


class SessionTests(AuthAPITestCase):

    def setUp(self):
        super().setUp()

        self.laptop = self.tokens()
        self.phone = self.tokens()

        self.other_user = User.objects.create_user(
            email="bob@example.com",
            password=PASSWORD,
            is_verified=True,
        )
        self.tokens("bob@example.com")

        self.authenticate(self.laptop["access"])

    def session_for(self, tokens):
        return UserSession.objects.get(
            refresh_jti=RefreshToken(tokens["refresh"])["jti"],
        )

    def test_sessions_require_authentication(self):
        self.client.credentials()

        response = self.client.get(reverse("sessions"))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_sessions_lists_only_own_sessions(self):
        response = self.client.get(reverse("sessions"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {session["id"] for session in response.json()},
            {
                str(session.id)
                for session in UserSession.objects.filter(user=self.user)
            },
        )
        self.assertEqual(len(response.json()), 2)

    def test_revoke_session_marks_it_revoked(self):
        session = self.session_for(self.phone)

        response = self.client.post(
            reverse("revoke-session", args=[session.id]),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        session.refresh_from_db()
        self.assertFalse(session.is_active)
        self.assertTrue(
            BlacklistedToken.objects.filter(
                token__jti=session.refresh_jti,
            ).exists()
        )
        self.assertTrue(self.session_for(self.laptop).is_active)

    def test_revoked_session_cannot_be_refreshed(self):
        session = self.session_for(self.phone)
        self.client.post(reverse("revoke-session", args=[session.id]))

        response = self.refresh(self.phone["refresh"])

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_revoke_session_of_other_user_returns_404(self):
        session = UserSession.objects.get(user=self.other_user)

        response = self.client.post(
            reverse("revoke-session", args=[session.id]),
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        session.refresh_from_db()
        self.assertTrue(session.is_active)

    def test_revoke_already_revoked_session_returns_404(self):
        session = self.session_for(self.phone)
        self.client.post(reverse("revoke-session", args=[session.id]))

        response = self.client.post(
            reverse("revoke-session", args=[session.id]),
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_revoke_all_sessions_revokes_only_own_sessions(self):
        response = self.client.post(reverse("revoke-all-sessions"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["revoked_sessions"], 2)
        self.assertFalse(
            UserSession.objects.filter(
                user=self.user,
                revoked_at__isnull=True,
            ).exists()
        )
        self.assertTrue(
            UserSession.objects.get(user=self.other_user).is_active
        )

    def test_revoked_all_sessions_cannot_be_refreshed(self):
        self.client.post(reverse("revoke-all-sessions"))

        for device, tokens in (("laptop", self.laptop), ("phone", self.phone)):
            with self.subTest(device=device):
                response = self.refresh(tokens["refresh"])

                self.assertEqual(
                    response.status_code,
                    status.HTTP_401_UNAUTHORIZED,
                )


@override_settings(
    JIVO_AUTH={
        "URL": settings.PUBLIC_URL,
        "ISSUER": settings.SIMPLE_JWT["ISSUER"],
        "PUBLIC_KEY": settings.JWT_VERIFYING_KEY,
        "APP": "oms",
    },
)
class JivoAuthClientTests(AuthAPITestCase):
    """The jivo-auth-client package accepts tokens issued by this service."""

    def setUp(self):
        super().setUp()

        clear_jwks_caches()

        Application.objects.create(slug="oms", name="OMS").users.add(self.user)

    def client_authenticate(self, token):
        request = APIRequestFactory().get(
            "/",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        return JivoJWTAuthentication().authenticate(request)

    def test_accepts_service_access_token(self):
        access = self.tokens()["access"]

        user, token = self.client_authenticate(access)

        self.assertEqual(user.id, self.user.id)
        self.assertEqual(user.pk, self.user.id)
        self.assertEqual(user.email, EMAIL)
        self.assertEqual(user.apps, ("oms",))
        self.assertTrue(user.is_authenticated)
        self.assertEqual(token, access)

    def test_accepts_refreshed_access_token(self):
        body = self.refresh(self.tokens()["refresh"]).json()

        user, _ = self.client_authenticate(body["access"])

        self.assertEqual(user.id, self.user.id)

    def test_rejects_refresh_token(self):
        with self.assertRaises(AuthenticationFailed):
            self.client_authenticate(self.tokens()["refresh"])

    def test_rejects_user_without_access_to_the_app(self):
        self.user.applications.clear()

        with self.assertRaises(PermissionDenied):
            self.client_authenticate(self.tokens()["access"])

    def test_rejects_token_from_another_issuer(self):
        access = self.tokens()["access"]

        with override_settings(
            JIVO_AUTH={
                **settings.JIVO_AUTH,
                "ISSUER": "https://someone-else.example",
            },
        ):
            with self.assertRaises(AuthenticationFailed):
                self.client_authenticate(access)

    def test_verifies_with_keys_from_jwks_endpoint(self):
        access = self.tokens()["access"]
        jwks = self.client.get(reverse("jwks")).json()

        with override_settings(
            JIVO_AUTH={
                **settings.JIVO_AUTH,
                "PUBLIC_KEY": None,
            },
        ):
            with mock.patch(
                "jivo_auth.keys.request_json",
                return_value=jwks,
            ) as fetch:
                user, _ = self.client_authenticate(access)

        self.assertEqual(user.id, self.user.id)
        fetch.assert_called_once()
        self.assertEqual(
            fetch.call_args.args[1],
            f"{settings.PUBLIC_URL}/.well-known/jwks.json",
        )

    def test_ignores_request_without_token(self):
        request = APIRequestFactory().get("/")

        self.assertIsNone(
            JivoJWTAuthentication().authenticate(request)
        )

    def test_failures_are_reported_as_401(self):
        request = APIRequestFactory().get("/")

        self.assertIsNotNone(
            JivoJWTAuthentication().authenticate_header(request)
        )


class JivoAuthClientLiveTests(LiveServerTestCase):
    """The client package against this service over real HTTP."""

    def setUp(self):
        cache.clear()
        clear_jwks_caches()
        self.addCleanup(clear_jwks_caches)

        self.user = User.objects.create_user(
            email=EMAIL,
            password=PASSWORD,
            is_verified=True,
        )

        oms = Application.objects.create(slug="oms", name="OMS")
        oms.users.add(self.user)

        settings_override = override_settings(
            JIVO_AUTH={
                "URL": self.live_server_url,
                "ISSUER": settings.SIMPLE_JWT["ISSUER"],
                "APP": "oms",
                "API_KEY": oms.set_new_api_key(),
            },
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

    def test_login_verify_lookup_refresh_logout(self):
        client = AuthClient()

        tokens = client.login(EMAIL, PASSWORD)

        # Fetches the signing key from /.well-known/jwks.json.
        claims = decode_access_token(tokens["access"])
        self.assertEqual(claims["sub"], str(self.user.id))
        self.assertEqual(claims["apps"], ["oms"])

        self.assertEqual(
            [user["email"] for user in client.get_users([self.user.id])],
            [EMAIL],
        )

        refreshed = client.refresh(tokens["refresh"])
        decode_access_token(refreshed["access"])

        client.logout(refreshed["refresh"])

        with self.assertRaises(AuthServiceError) as caught:
            client.refresh(refreshed["refresh"])

        self.assertEqual(caught.exception.status, 401)

    def test_wrong_password_is_401(self):
        with self.assertRaises(AuthServiceError) as caught:
            AuthClient().login(EMAIL, "Wrong!Pass1")

        self.assertEqual(caught.exception.status, 401)
