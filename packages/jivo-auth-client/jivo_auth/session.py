"""
Jivo Auth tokens of users logged in through a Django session
(JivoAuthBackend), kept in the session so JivoSessionMiddleware can refresh
them and notice when Jivo Auth ends the session.
"""

import hashlib
import time

import jwt
from django.core.cache import cache

from . import settings as conf
from .client import AuthClient
from .exceptions import AuthServiceUnavailable


SESSION_KEY = "_jivo_auth"

# How long another request's refresh result stays available.
REFRESH_RESULT_TTL = 60


def store_tokens(request, tokens):
    # The access token was verified before it got here.
    claims = jwt.decode(
        tokens["access"],
        options={"verify_signature": False},
    )

    request.session[SESSION_KEY] = {
        "access": tokens["access"],
        "refresh": tokens["refresh"],
        "access_exp": claims["exp"],
    }


def get_tokens(request):
    return request.session.get(SESSION_KEY)


def get_access_token(request):
    """
    The logged-in user's current Jivo Auth access token, for calling other
    Jivo APIs on their behalf, or None.
    """

    tokens = get_tokens(request)

    return tokens["access"] if tokens else None


def access_token_expires_soon(tokens, margin):
    return tokens["access_exp"] - time.time() <= margin


def refresh_tokens(refresh_token, request=None):
    """
    Exchange a refresh token for new tokens.

    Refresh tokens rotate, so when several requests of one session need a
    refresh at once, only the first calls Jivo Auth and the others wait for
    its result in the cache. With a per-process cache (the default
    LocMemCache) requests in other processes refresh too; Jivo Auth then
    answers the just-rotated token with the same new tokens for a short
    grace period, so the user stays logged in either way.
    """

    digest = hashlib.sha256(refresh_token.encode()).hexdigest()
    result_key = f"jivo_auth:refreshed:{digest}"
    lock_key = f"jivo_auth:refreshing:{digest}"
    wait = conf.get("TIMEOUT") + 5

    result = cache.get(result_key)

    if result:
        return result

    if cache.add(lock_key, True, timeout=wait):
        try:
            result = AuthClient().refresh(refresh_token, request=request)
            cache.set(result_key, result, timeout=REFRESH_RESULT_TTL)
        finally:
            cache.delete(lock_key)

        return result

    deadline = time.monotonic() + wait

    while time.monotonic() < deadline:
        time.sleep(0.05)

        result = cache.get(result_key)

        if result:
            return result

        # The other request finished without a result: its refresh failed.
        if not cache.get(lock_key):
            break

    raise AuthServiceUnavailable("a concurrent token refresh did not finish")
