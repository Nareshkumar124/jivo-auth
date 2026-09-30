import logging

from django.apps import apps
from django.contrib.auth import logout
from django.core.exceptions import ImproperlyConfigured

from . import settings as conf
from .exceptions import AuthServiceError, AuthServiceUnavailable, InvalidToken
from .session import (
    access_token_expires_soon,
    get_tokens,
    refresh_tokens,
    store_tokens,
)
from .tokens import decode_access_token, has_app_access


logger = logging.getLogger(__name__)

# Refresh this many seconds before the access token expires.
REFRESH_MARGIN = 30


class JivoSessionMiddleware:
    """
    Keeps the Jivo Auth session of users logged in with JivoAuthBackend in
    step with their Django session. Shortly before the access token expires
    it refreshes the tokens; if Jivo Auth refuses (the user logged out
    elsewhere, changed their password, was deactivated or lost access to
    this application) the user is logged out here too.

    Place it after AuthenticationMiddleware.
    """

    def __init__(self, get_response):
        if not apps.is_installed("jivo_auth"):
            raise ImproperlyConfigured(
                'JivoSessionMiddleware needs "jivo_auth" in INSTALLED_APPS.'
            )

        self.get_response = get_response

    def __call__(self, request):
        self.sync(request)

        return self.get_response(request)

    def sync(self, request):

        if not hasattr(request, "user"):
            raise ImproperlyConfigured(
                "JivoSessionMiddleware must come after "
                "django.contrib.auth.middleware.AuthenticationMiddleware."
            )

        tokens = get_tokens(request)

        # Users who logged in some other way (e.g. a local superuser) have
        # no Jivo Auth session to follow.
        if not tokens or not request.user.is_authenticated:
            return

        if not access_token_expires_soon(tokens, REFRESH_MARGIN):
            return

        try:
            new_tokens = refresh_tokens(tokens["refresh"], request=request)

        except AuthServiceUnavailable as exc:
            # Keep the user logged in; try again on the next request.
            logger.warning("Could not refresh Jivo Auth tokens: %s", exc)
            return

        except AuthServiceError as exc:
            if exc.status == 401:
                self.log_out(request, remote_session_ended=True)
            else:
                logger.warning("Could not refresh Jivo Auth tokens: %s", exc)

            return

        try:
            claims = decode_access_token(new_tokens["access"])
        except InvalidToken:
            logger.exception("Could not verify refreshed Jivo Auth tokens.")
            return

        id_field = conf.get("LOCAL_USER_ID_FIELD")

        if (
            not has_app_access(claims)
            or str(getattr(request.user, id_field)) != str(claims["sub"])
        ):
            store_tokens(request, new_tokens)
            self.log_out(request, remote_session_ended=False)
            return

        store_tokens(request, new_tokens)

    def log_out(self, request, remote_session_ended):
        # Read by the user_logged_out receiver, which otherwise ends the
        # Jivo Auth session too.
        request._jivo_auth_session_ended = remote_session_ended

        logout(request)
