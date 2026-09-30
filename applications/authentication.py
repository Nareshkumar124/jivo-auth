from rest_framework import authentication, permissions
from rest_framework.exceptions import AuthenticationFailed

from .models import Application


# Header carrying an application's API key on server-to-server calls.
APP_KEY_HEADER = "HTTP_X_JIVO_APP_KEY"


def get_request_application(request):
    """
    The active Application whose API key the request carries, or None.
    Looked up once per request.
    """

    http_request = getattr(request, "_request", request)

    if not hasattr(http_request, "_jivo_application"):
        http_request._jivo_application = Application.from_api_key(
            http_request.META.get(APP_KEY_HEADER, "")
        )

    return http_request._jivo_application


class ApplicationPrincipal:
    """request.user for a request authenticated with an API key."""

    is_authenticated = True
    is_anonymous = False

    def __init__(self, application):
        self.application = application

    # DRF's user and scoped throttles key on request.user.pk.
    @property
    def pk(self):
        return f"app:{self.application.pk}"

    def __str__(self):
        return self.application.slug


class ApplicationKeyAuthentication(authentication.BaseAuthentication):
    """Authenticates an application by the X-Jivo-App-Key header."""

    def authenticate(self, request):

        if not request.META.get(APP_KEY_HEADER):
            return None

        application = get_request_application(request)

        if application is None:
            raise AuthenticationFailed(
                "Invalid application API key."
            )

        return ApplicationPrincipal(application), application

    # Without this DRF answers failed authentication with 403, not 401.
    def authenticate_header(self, request):
        return "X-Jivo-App-Key"


class IsApplication(permissions.BasePermission):

    def has_permission(self, request, view):
        return isinstance(request.auth, Application)
