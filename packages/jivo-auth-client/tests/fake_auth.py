"""
A stand-in for the Jivo Auth service, installed in place of the client's
urlopen. It issues RS256 tokens shaped like the real service's and records
every call.
"""

import io
import json
import secrets
import time
import uuid
from contextlib import contextmanager
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from jivo_auth.keys import clear_jwks_caches


URL = "https://auth.example.test"
API_KEY = "jivo_test-api-key"
PASSWORD = "Str0ng!Pass"


def new_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_pem(private_key):
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


class FakeResponse(io.BytesIO):

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class FakeAuthService:

    def __init__(self):
        self.private_key = new_rsa_key()
        self.kid = "test-key-1"

        self.users = {}
        self.sessions = {}
        self.calls = []
        self.down = False

    # Test data ------------------------------------------------------------

    def add_user(self, email, apps=("oms",), active=True):
        self.users[email] = {
            "id": str(uuid.uuid4()),
            "email": email,
            "apps": list(apps),
            "active": active,
        }

        return self.users[email]

    def access_token(self, user, *, key=None, kid=None, **overrides):
        now = int(time.time())

        claims = {
            "token_type": "access",
            "exp": now + 900,
            "iat": now,
            "jti": secrets.token_hex(16),
            "sub": user["id"],
            "email": user["email"],
            "apps": user["apps"],
            "iss": URL,
            **overrides,
        }

        return jwt.encode(
            {name: value for name, value in claims.items() if value is not None},
            key or self.private_key,
            algorithm="RS256",
            headers={"kid": kid or self.kid},
        )

    def tokens(self, user, **overrides):
        refresh = secrets.token_urlsafe(24)
        self.sessions[refresh] = user["email"]

        return {
            "access": self.access_token(user, **overrides),
            "refresh": refresh,
        }

    def jwks(self):
        jwk = RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)

        return {
            "keys": [
                {**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"},
            ],
        }

    def calls_to(self, path):
        return [call for call in self.calls if call["path"] == path]

    # Transport ------------------------------------------------------------

    def __call__(self, request, timeout=None):
        url = urlsplit(request.full_url)

        call = {
            "method": request.get_method(),
            "path": url.path,
            "query": parse_qs(url.query),
            "headers": {
                name.lower(): value
                for name, value in request.header_items()
            },
            "data": json.loads(request.data) if request.data else None,
        }

        self.calls.append(call)

        if self.down:
            raise URLError("connection refused")

        status, body = self.handle(call)

        payload = json.dumps(body).encode()

        if status >= 400:
            raise HTTPError(
                request.full_url,
                status,
                "error",
                {},
                io.BytesIO(payload),
            )

        return FakeResponse(payload)

    def handle(self, call):
        route = (call["method"], call["path"])
        data = call["data"] or {}

        if route == ("GET", "/.well-known/jwks.json"):
            return 200, self.jwks()

        if route == ("POST", "/api/v1/auth/login/"):
            user = self.users.get(data.get("email"))

            if not user or not user["active"] or data.get("password") != PASSWORD:
                return 401, {
                    "detail": "No active account found with the given credentials",
                }

            return 200, self.tokens(user)

        if route == ("POST", "/api/v1/auth/refresh/"):
            email = self.sessions.pop(data.get("refresh"), None)
            user = self.users.get(email)

            if not user or not user["active"]:
                return 401, {
                    "detail": "Token is blacklisted",
                    "code": "token_not_valid",
                }

            return 200, self.tokens(user)

        if route == ("POST", "/api/v1/auth/logout/"):
            if self.sessions.pop(data.get("refresh"), None) is None:
                return 400, {"refresh": ["Invalid refresh token."]}

            return 200, {"message": "Logged out successfully."}

        if route == ("GET", "/api/v1/apps/users/"):
            if call["headers"].get("x-jivo-app-key") != API_KEY:
                return 401, {"detail": "Invalid application API key."}

            ids = set(call["query"].get("id", []))

            return 200, [
                {
                    "id": user["id"],
                    "email": user["email"],
                    "first_name": "",
                    "last_name": "",
                    "is_active": user["active"],
                }
                for user in self.users.values()
                if "oms" in user["apps"] and (not ids or user["id"] in ids)
            ]

        return 404, {"detail": "Not found."}


@contextmanager
def fake_auth_service():
    service = FakeAuthService()

    clear_jwks_caches()

    with mock.patch("jivo_auth.http.urlopen", service):
        yield service

    clear_jwks_caches()
