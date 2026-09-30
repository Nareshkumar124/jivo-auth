import logging

from django.contrib.auth.backends import ModelBackend

from .client import AuthClient
from .exceptions import AuthServiceError, AuthServiceUnavailable, InvalidToken
from .tokens import decode_access_token, has_app_access
from .users import get_local_user


logger = logging.getLogger(__name__)


def end_remote_session(client, tokens):
    try:
        client.logout(tokens["refresh"])
    except AuthServiceError as exc:
        logger.warning("Could not end the Jivo Auth session: %s", exc)


class JivoAuthBackend(ModelBackend):
    """
    Checks email and password with Jivo Auth, so Django's login views and
    the admin log users in with their Jivo account. The user is a local
    user record (see JIVO_AUTH['LOCAL_USER_ID_FIELD']), created on first
    login; staff status, groups and permissions stay local to this app.

    Needs "jivo_auth" in INSTALLED_APPS and JivoSessionMiddleware, which
    keep the Jivo Auth session in step with the Django session.
    """

    def authenticate(self, request, username=None, password=None, email=None, **kwargs):

        email = email or username

        if not email or not password:
            return None

        client = AuthClient()

        try:
            tokens = client.login(email, password, request=request)

        except AuthServiceUnavailable as exc:
            logger.error("Jivo Auth login failed: %s", exc)
            return None

        except AuthServiceError as exc:
            # 401 is a wrong email or password; anything else is worth logging.
            if exc.status != 401:
                logger.warning("Jivo Auth login failed: %s", exc)

            return None

        try:
            claims = decode_access_token(tokens["access"])

        except InvalidToken:
            logger.exception(
                "Jivo Auth issued a token this application can't verify; "
                "check the JIVO_AUTH settings."
            )
            end_remote_session(client, tokens)
            return None

        if not has_app_access(claims):
            end_remote_session(client, tokens)
            return None

        user = get_local_user(claims)

        if not self.user_can_authenticate(user):
            end_remote_session(client, tokens)
            return None

        # Stored in the session by the user_logged_in receiver, once login()
        # has settled which session the user gets.
        user._jivo_tokens = tokens

        return user
