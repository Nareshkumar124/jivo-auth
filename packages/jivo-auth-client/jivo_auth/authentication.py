from rest_framework import authentication
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied

from . import settings as conf
from .exceptions import InvalidToken
from .tokens import decode_access_token, has_app_access
from .users import JivoUser, get_local_user


class JivoJWTAuthentication(
    authentication.BaseAuthentication
):
    """
    Authenticates `Authorization: Bearer <access token>` with a token from
    Jivo Auth, verified locally. request.user is a JivoUser, or the local
    user record with JIVO_AUTH['LOCAL_USERS'].
    """

    # Without this DRF answers failed authentication with 403, not 401.
    def authenticate_header(self, request):
        return 'Bearer realm="api"'

    def authenticate(self, request):

        header = authentication.get_authorization_header(
            request
        )

        parts = header.split()

        # Not a bearer token: leave it to other authentication classes.
        if not parts or parts[0].lower() != b"bearer":
            return None

        if len(parts) != 2:
            raise AuthenticationFailed(
                "Invalid authorization header."
            )

        try:
            token = parts[1].decode()
        except UnicodeError as exc:
            raise AuthenticationFailed(
                "Invalid authorization header."
            ) from exc

        try:
            claims = decode_access_token(token)
        except InvalidToken as exc:
            raise AuthenticationFailed(str(exc)) from exc

        if not has_app_access(claims):
            raise PermissionDenied(
                "Your account does not have access to this application."
            )

        if not conf.get("LOCAL_USERS"):
            return JivoUser.from_claims(claims), token

        user = get_local_user(claims)

        if not user.is_active:
            raise AuthenticationFailed(
                "User account is disabled."
            )

        return user, token


try:
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

except ImportError:
    pass

else:

    class JivoJWTScheme(OpenApiAuthenticationExtension):
        target_class = "jivo_auth.authentication.JivoJWTAuthentication"
        name = "jivoAuth"

        def get_security_definition(self, auto_schema):
            return {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "Access token from Jivo Auth.",
            }
