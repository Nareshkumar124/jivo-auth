"""
Configuration, read from the JIVO_AUTH dict in Django settings, then from
JIVO_AUTH_<NAME> environment variables, then the defaults below:

URL                  Jivo Auth's base URL, e.g. https://auth.jivo.in. The
                     default issuer, JWKS location and API base.
APP                  This application's slug. Tokens whose `apps` claim
                     doesn't list it are refused with 403. Required unless
                     ALLOW_ALL_USERS is set.
ALLOW_ALL_USERS      If true and APP is unset, accept every Jivo Auth user.
API_KEY              This application's API key, for logging users in from
                     the server and for looking users up.
NUM_PROXIES          Reverse proxies in front of this application that add
                     to X-Forwarded-For (0: use REMOTE_ADDR). Decides which
                     end-user IP is reported to Jivo Auth.
ISSUER               Required `iss` claim. Defaults to URL; "" disables.
ALGORITHM            RS256, or HS256 when SECRET is set.
JWKS_URL             Defaults to URL + /.well-known/jwks.json.
PUBLIC_KEY           PEM public key; verifies without fetching the JWKS.
SECRET               The shared JWT_SECRET_KEY, when Jivo Auth uses HS256.
LEEWAY               Seconds of clock difference to tolerate (10).
TIMEOUT              Seconds to wait for Jivo Auth (5).
LOCAL_USERS          If true, JivoJWTAuthentication returns a local user
                     record (created on first sight) instead of a JivoUser.
LOCAL_USER_ID_FIELD  Field of the local user model holding the Jivo user ID
                     ("username"), e.g. an `auth_id` UUIDField.
LOCAL_USER_LINK_BY_EMAIL
                     If true, a user's first token links the one local record
                     with the same email and no Jivo ID, instead of creating
                     a new record. For applications whose users existed
                     before Jivo Auth (False).
"""

import os

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


DEFAULTS = {
    "URL": None,
    "APP": None,
    "ALLOW_ALL_USERS": False,
    "API_KEY": None,
    "NUM_PROXIES": 0,
    "ISSUER": None,
    "ALGORITHM": None,
    "JWKS_URL": None,
    "PUBLIC_KEY": None,
    "SECRET": None,
    "LEEWAY": 10,
    "TIMEOUT": 5,
    "LOCAL_USERS": False,
    "LOCAL_USER_ID_FIELD": "username",
    "LOCAL_USER_LINK_BY_EMAIL": False,
}

FLOAT_SETTINGS = {"LEEWAY", "TIMEOUT"}
INT_SETTINGS = {"NUM_PROXIES"}
BOOL_SETTINGS = {"LOCAL_USERS", "ALLOW_ALL_USERS", "LOCAL_USER_LINK_BY_EMAIL"}


def unknown_settings():
    return sorted(set(getattr(settings, "JIVO_AUTH", {})) - set(DEFAULTS))


def get(name):
    config = getattr(settings, "JIVO_AUTH", {})

    if name in config:
        return config[name]

    value = os.getenv(f"JIVO_AUTH_{name}")

    if value is None or value == "":
        # An explicitly empty ISSUER means "don't check the issuer".
        if name == "ISSUER" and value == "":
            return ""

        return DEFAULTS[name]

    if name in FLOAT_SETTINGS | INT_SETTINGS:
        try:
            return int(value) if name in INT_SETTINGS else float(value)
        except ValueError as exc:
            raise ImproperlyConfigured(
                f"JIVO_AUTH_{name} must be a number."
            ) from exc

    if name in BOOL_SETTINGS:
        return value.strip().lower() in ("1", "true", "yes", "on")

    return value


def auth_url():
    url = get("URL")

    return url.rstrip("/") if url else None


def require_auth_url():
    url = auth_url()

    if not url:
        raise ImproperlyConfigured(
            "Set JIVO_AUTH['URL'] to Jivo Auth's base URL, e.g. "
            "https://auth.jivo.in."
        )

    return url


def algorithm():
    return get("ALGORITHM") or ("HS256" if get("SECRET") else "RS256")


def issuer():
    value = get("ISSUER")

    if value is None:
        return auth_url()

    return value or None


def jwks_url():
    url = get("JWKS_URL")

    if url:
        return url

    base = auth_url()

    return f"{base}/.well-known/jwks.json" if base else None
