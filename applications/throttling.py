"""
Client identity for throttles and session records.

An application that logs users in from its own server (instead of from the
user's browser) sends every login from the same IP. When the request carries
a valid application API key, the end user's IP in X-Jivo-Client-IP is used
instead, so users of that application are throttled separately.
"""

import hashlib
import ipaddress

from rest_framework import throttling

from .authentication import get_request_application


CLIENT_IP_HEADER = "HTTP_X_JIVO_CLIENT_IP"


def _is_ip(value):
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False

    return True


def get_client_ident(request):
    """
    The end user's IP from a trusted application, otherwise REMOTE_ADDR or
    the X-Forwarded-For entry added by a trusted proxy
    (REST_FRAMEWORK["NUM_PROXIES"]).
    """

    forwarded = request.META.get(CLIENT_IP_HEADER, "").strip()

    if (
        forwarded
        and _is_ip(forwarded)
        and get_request_application(request) is not None
    ):
        return forwarded

    return throttling.BaseThrottle().get_ident(request)


def get_client_ip(request):
    ident = get_client_ident(request)

    return str(ipaddress.ip_address(ident)) if _is_ip(ident) else None


class ClientIdentMixin:

    def get_ident(self, request):
        return get_client_ident(request)


class AnonRateThrottle(ClientIdentMixin, throttling.AnonRateThrottle):
    pass


class UserRateThrottle(ClientIdentMixin, throttling.UserRateThrottle):
    pass


class ScopedRateThrottle(ClientIdentMixin, throttling.ScopedRateThrottle):
    pass


class LoginRateThrottle(ClientIdentMixin, throttling.SimpleRateThrottle):
    """
    Login attempts per account and client IP ("login" rate), so one person
    guessing a password doesn't lock out everyone behind the same IP.
    """

    scope = "login"

    def get_cache_key(self, request, view):
        data = request.data if hasattr(request.data, "get") else {}
        email = str(data.get("email", "")).strip().lower()

        account = hashlib.sha256(email.encode()).hexdigest()[:32]

        return self.cache_format % {
            "scope": self.scope,
            "ident": f"{self.get_ident(request)}:{account}",
        }


class LoginIPRateThrottle(ClientIdentMixin, throttling.SimpleRateThrottle):
    """All login attempts from one client IP ("login_ip" rate)."""

    scope = "login_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }
