import ipaddress

from django.core.exceptions import ImproperlyConfigured

from . import settings as conf
from .http import request_json


USER_IDS_PER_REQUEST = 100


def end_user_ip(request):
    """
    The end user's IP address, or None. X-Forwarded-For is only read when
    JIVO_AUTH['NUM_PROXIES'] says proxies add to it, and then only the entry
    the outermost of them added: anything before that is client-supplied.
    """

    ident = request.META.get("REMOTE_ADDR", "")
    num_proxies = conf.get("NUM_PROXIES")

    if num_proxies > 0:
        addrs = [
            addr.strip()
            for addr in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
            if addr.strip()
        ]

        if addrs:
            ident = addrs[-min(num_proxies, len(addrs))]

    try:
        return str(ipaddress.ip_address(ident))
    except ValueError:
        return None


def end_user_headers(request):
    """
    Headers that let Jivo Auth rate-limit and record the end user rather
    than this server. X-Jivo-Client-IP is only trusted with an API key.
    """

    if request is None:
        return {}

    headers = {}

    user_agent = request.META.get("HTTP_USER_AGENT")

    if user_agent:
        headers["User-Agent"] = user_agent

    ip = end_user_ip(request)

    if ip:
        headers["X-Jivo-Client-IP"] = ip

    return headers


class AuthClient:
    """Calls to the Jivo Auth API made from this application's server."""

    def __init__(self):
        self.url = conf.require_auth_url()
        self.api_key = conf.get("API_KEY")
        self.timeout = conf.get("TIMEOUT")

    def _call(self, method, path, *, data=None, params=None, request=None):
        headers = {}

        if self.api_key:
            headers["X-Jivo-App-Key"] = self.api_key
            headers.update(end_user_headers(request))

        return request_json(
            method,
            f"{self.url}{path}",
            data=data,
            params=params,
            headers=headers,
            timeout=self.timeout,
        )

    def login(self, email, password, request=None):
        """Returns {"access": ..., "refresh": ...}."""

        return self._call(
            "POST",
            "/api/v1/auth/login/",
            data={
                "email": email,
                "password": password,
                "device_name": conf.get("APP") or "",
            },
            request=request,
        )

    def refresh(self, refresh_token, request=None):
        """Returns new {"access": ..., "refresh": ...}; the old refresh token
        stops working."""

        return self._call(
            "POST",
            "/api/v1/auth/refresh/",
            data={"refresh": refresh_token},
            request=request,
        )

    def logout(self, refresh_token):
        self._call(
            "POST",
            "/api/v1/auth/logout/",
            data={"refresh": refresh_token},
        )

    def get_users(self, ids=None):
        """
        Users with access to this application (id, email, first_name,
        last_name, is_active), optionally only those with the given IDs.
        Needs JIVO_AUTH['API_KEY'].
        """

        if not self.api_key:
            raise ImproperlyConfigured(
                "JIVO_AUTH['API_KEY'] is required to look up users."
            )

        if ids is None:
            return self._call("GET", "/api/v1/apps/users/")

        ids = [str(user_id) for user_id in ids]
        users = []

        # The API takes at most this many IDs per request.
        for start in range(0, len(ids), USER_IDS_PER_REQUEST):
            users.extend(
                self._call(
                    "GET",
                    "/api/v1/apps/users/",
                    params={"id": ids[start:start + USER_IDS_PER_REQUEST]},
                )
            )

        return users
