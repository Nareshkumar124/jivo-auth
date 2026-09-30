from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from .models import UserSession


def revoke_sessions(sessions):
    """
    Blacklist the refresh tokens of the given UserSession queryset and mark
    the sessions revoked. Returns the number of sessions revoked.
    """

    sessions = sessions.filter(
        revoked_at__isnull=True,
    )

    outstanding_tokens = OutstandingToken.objects.filter(
        jti__in=sessions.values("refresh_jti"),
    )

    BlacklistedToken.objects.bulk_create(
        [
            BlacklistedToken(token=token)
            for token in outstanding_tokens
        ],
        ignore_conflicts=True,
    )

    return sessions.update(
        revoked_at=timezone.now(),
    )


def revoke_all_sessions(user):
    return revoke_sessions(
        UserSession.objects.filter(user=user)
    )
