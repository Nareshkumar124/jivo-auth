from django.contrib.auth import get_user_model

from .context import current_request
from .models import AuditEvent


def _request_actor(request):
    # DRF copies the token's user onto the Django request once it has
    # authenticated, so this also works for API calls.
    user = getattr(request, "user", None)

    if user is not None and getattr(user, "is_authenticated", False):
        if isinstance(user, get_user_model()):
            return user

    return None


def record(
    type,
    *,
    user=None,
    actor=None,
    email="",
    application=None,
    request=None,
    **details,
):
    """
    Save an AuditEvent. The client IP, user agent and (for events caused by
    a signed-in administrator) the actor come from the current request.
    """

    from applications.throttling import get_client_ip

    request = request or current_request()

    if actor is None and request is not None:
        actor = _request_actor(request)

    # The user acting on their own account isn't "someone else".
    if actor is not None and user is not None and actor.pk == user.pk:
        actor = None

    return AuditEvent.objects.create(
        type=type,
        user=user,
        actor=actor,
        email=(email or (user.email if user is not None else ""))[:254],
        application=application,
        ip_address=get_client_ip(request) if request is not None else None,
        user_agent=(
            request.META.get("HTTP_USER_AGENT", "")[:255]
            if request is not None
            else ""
        ),
        details=details,
    )
