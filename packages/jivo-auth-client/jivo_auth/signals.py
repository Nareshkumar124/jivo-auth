import logging

from .client import AuthClient
from .exceptions import AuthServiceError
from .session import get_tokens, store_tokens


logger = logging.getLogger(__name__)


def store_tokens_on_login(sender, request, user, **kwargs):
    tokens = getattr(user, "_jivo_tokens", None)

    if tokens is None or request is None:
        return

    store_tokens(request, tokens)

    del user._jivo_tokens


def end_remote_session_on_logout(sender, request, user, **kwargs):
    # Sent before logout() clears the session, so the tokens are still there.
    if request is None or getattr(request, "_jivo_auth_session_ended", False):
        return

    tokens = get_tokens(request)

    if not tokens:
        return

    try:
        AuthClient().logout(tokens["refresh"])
    except AuthServiceError as exc:
        logger.warning("Could not end the Jivo Auth session: %s", exc)
