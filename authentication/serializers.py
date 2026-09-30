from django.conf import settings
from django.contrib.auth.models import update_last_login
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers
from rest_framework_simplejwt import state as jwt_state

# SimpleJWT's AuthenticationFailed puts `code` in the response body.
from rest_framework_simplejwt.exceptions import (
    AuthenticationFailed,
    InvalidToken,
    TokenBackendError,
    TokenError,
)
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer,
    TokenObtainSerializer,
    TokenRefreshSerializer,
)
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

# Same identity the throttles use: REST_FRAMEWORK["NUM_PROXIES"] decides
# which X-Forwarded-For entry (if any) is trusted, and a trusted application
# may name the end user's IP.
from applications.throttling import get_client_ip

from .models import UserSession
from .services import revoke_sessions
from .tokens import issue_tokens


EMAIL_NOT_VERIFIED = "Email address is not verified."


def check_can_get_tokens(user):
    """Raise AuthenticationFailed unless tokens may be issued to the user."""

    if not api_settings.USER_AUTHENTICATION_RULE(user):
        raise AuthenticationFailed(
            TokenRefreshSerializer.default_error_messages["no_active_account"],
            "no_active_account",
        )

    if settings.REQUIRE_VERIFIED_EMAIL and not user.is_verified:
        raise AuthenticationFailed(
            EMAIL_NOT_VERIFIED,
            "email_not_verified",
        )


class LoginSerializer(TokenObtainPairSerializer):

    device_name = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        write_only=True,
        help_text=(
            "Optional label for this device, shown in the session list "
            "(e.g. \"Alice's MacBook\")."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # SimpleJWT adds these fields at runtime; describe them for the docs.
        self.fields[self.username_field].help_text = (
            "Account email address. Case-insensitive."
        )
        self.fields["password"].help_text = "Account password."

    def validate(self, attrs):

        # Checks the credentials and is_active and sets self.user, without
        # issuing tokens yet (TokenObtainPairSerializer.validate would).
        TokenObtainSerializer.validate(self, attrs)

        # Only after the password matched, so it reveals nothing to others.
        check_can_get_tokens(self.user)

        refresh = issue_tokens(self.user)

        if api_settings.UPDATE_LAST_LOGIN:
            update_last_login(None, self.user)

        request = self.context["request"]

        UserSession.objects.create(
            user=self.user,
            refresh_jti=refresh["jti"],
            device_name=attrs.get("device_name", ""),
            ip_address=get_client_ip(request),
            user_agent=request.META.get(
                "HTTP_USER_AGENT",
                "",
            ),
        )

        return {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
        }


@extend_schema_serializer(component_name="TokenRefresh")
class SessionTokenRefreshSerializer(TokenRefreshSerializer):
    """
    Refresh only works for a refresh token that belongs to an active
    UserSession, and moves that session onto the rotated token.

    The token a session was just rotated from is accepted again for
    settings.REFRESH_TOKEN_REUSE_GRACE and gets the same new tokens, so a
    lost response or two racing requests don't log the user out. Using it
    later revokes the session, since only a copy of the token can still be
    sending it.
    """

    refresh = serializers.CharField(
        help_text="Refresh token from login or from the previous refresh.",
    )

    def verified_token(self, raw_token):
        """
        The refresh token and whether it is blacklisted. Raises TokenError
        for anything else that makes it invalid (the view answers 401).
        """

        try:
            return self.token_class(raw_token), False
        except TokenError as exc:
            original_error = exc

        # Blacklisted tokens fail above; check everything else, so a
        # rotated token can be recognized as a replay.
        try:
            jwt_state.token_backend.decode(raw_token, verify=True)
            token = self.token_class(raw_token, verify=False)
            token.verify_token_type()
        except (TokenBackendError, TokenError):
            raise original_error

        return token, True

    def validate(self, attrs):

        old_refresh, blacklisted = self.verified_token(attrs["refresh"])
        jti = old_refresh["jti"]
        reused = False

        with transaction.atomic():

            # The row lock makes concurrent refreshes of the same token
            # queue up; the later ones find it as the previous token.
            session = (
                UserSession.objects
                .select_for_update(of=("self",))
                .select_related("user")
                .filter(
                    Q(refresh_jti=jti) | Q(previous_refresh_jti=jti),
                    revoked_at__isnull=True,
                )
                .first()
            )

            if session is None:
                raise InvalidToken(
                    "Session has been revoked."
                )

            check_can_get_tokens(session.user)

            if session.refresh_jti == jti and not blacklisted:
                return self.rotate(session, old_refresh)

            if session.previous_refresh_jti == jti:
                grace_start = timezone.now() - settings.REFRESH_TOKEN_REUSE_GRACE

                if session.rotated_at and session.rotated_at >= grace_start:
                    return self.current_tokens(session)

                revoke_sessions(
                    UserSession.objects.filter(pk=session.pk)
                )
                reused = True

        # Raised outside the transaction, so the revocation is kept.
        raise InvalidToken(
            "Token was already used; the session has been revoked."
            if reused
            else "Token is blacklisted"
        )

    def rotate(self, session, old_refresh):

        old_refresh.blacklist()

        # Fresh tokens rather than copies of the old one, so the claims
        # follow changes to the user's email and application access.
        refresh = issue_tokens(session.user)

        now = timezone.now()

        session.previous_refresh_jti = old_refresh["jti"]
        session.refresh_jti = refresh["jti"]
        session.rotated_at = now
        session.last_used_at = now

        session.save(
            update_fields=[
                "previous_refresh_jti",
                "refresh_jti",
                "rotated_at",
                "last_used_at",
            ]
        )

        return {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        }

    def current_tokens(self, session):

        stored = (
            OutstandingToken.objects
            .filter(jti=session.refresh_jti)
            .values_list("token", flat=True)
            .first()
        )

        if stored is None:
            raise InvalidToken(
                "Session has been revoked."
            )

        refresh = self.token_class(stored)

        return {
            "access": str(refresh.access_token),
            "refresh": stored,
        }


class LogoutSerializer(serializers.Serializer):

    refresh = serializers.CharField(
        write_only=True,
        help_text="Refresh token of the session to end.",
    )

    def validate_refresh(self, value):

        # Holding the refresh token is proof enough to end its session.
        try:
            return RefreshToken(value)
        except TokenError as exc:
            raise serializers.ValidationError(
                "Invalid refresh token."
            ) from exc


class UserSessionSerializer(serializers.ModelSerializer):

    is_active = serializers.BooleanField(
        read_only=True,
        help_text="`false` once the session has been logged out or revoked.",
    )

    class Meta:
        model = UserSession
        fields = [
            "id",
            "device_name",
            "ip_address",
            "user_agent",
            "created_at",
            "last_used_at",
            "revoked_at",
            "is_active",
        ]
        read_only_fields = fields
        extra_kwargs = {
            "id": {
                "help_text": "Session ID, used to revoke this session.",
            },
            "device_name": {
                "help_text": "Device label given at login, if any.",
            },
            "ip_address": {
                "help_text": "Client IP address at login, if known.",
            },
            "user_agent": {
                "help_text": "User-Agent header sent at login.",
            },
            "created_at": {
                "help_text": "When the user logged in.",
            },
            "last_used_at": {
                "help_text": "When the session's tokens were last refreshed.",
            },
            "revoked_at": {
                "help_text": "When the session was logged out or revoked.",
            },
        }
