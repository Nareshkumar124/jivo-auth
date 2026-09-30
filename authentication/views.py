from django.conf import settings
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)

from applications.throttling import (
    AnonRateThrottle,
    LoginIPRateThrottle,
    LoginRateThrottle,
)
from audit.models import AuditEvent
from audit.services import record
from users.models import User

from .models import UserSession
from .schema import (
    jwks_schema,
    login_schema,
    logout_schema,
    refresh_schema,
    revoke_all_sessions_schema,
    revoke_session_schema,
    session_list_schema,
    verify_schema,
)
from .serializers import (
    LoginSerializer,
    LogoutSerializer,
    SessionTokenRefreshSerializer,
    UserSessionSerializer,
)
from .services import revoke_all_sessions, revoke_sessions


@login_schema
class LoginView(TokenObtainPairView):

    serializer_class = LoginSerializer

    permission_classes = [
        permissions.AllowAny
    ]

    throttle_classes = [
        AnonRateThrottle,
        LoginRateThrottle,
        LoginIPRateThrottle,
    ]


@refresh_schema
class RefreshTokenView(TokenRefreshView):

    serializer_class = SessionTokenRefreshSerializer

    permission_classes = [
        permissions.AllowAny
    ]


@verify_schema
class VerifyTokenView(TokenVerifyView):

    permission_classes = [
        permissions.AllowAny
    ]


@jwks_schema
class JWKSView(APIView):

    authentication_classes = []
    permission_classes = []

    def get(self, request):

        jwk = settings.JWT_PUBLIC_JWK

        response = Response(
            {
                "keys": [jwk] if jwk else [],
            }
        )

        # Short, so a caching proxy can't hide a rotated key for long.
        response["Cache-Control"] = "public, max-age=300"

        return response


@logout_schema
class LogoutView(generics.GenericAPIView):

    serializer_class = LogoutSerializer

    # The refresh token proves the session, so logging out still works once
    # the access token has expired; an Authorization header is ignored.
    authentication_classes = []

    permission_classes = [
        permissions.AllowAny
    ]

    def post(self, request):

        serializer = self.get_serializer(
            data=request.data
        )

        serializer.is_valid(raise_exception=True)

        token = serializer.validated_data["refresh"]

        token.blacklist()

        revoke_sessions(
            UserSession.objects.filter(
                refresh_jti=token["jti"],
            )
        )

        record(
            AuditEvent.Type.LOGOUT,
            user=User.objects.filter(pk=token.get("sub")).first(),
        )

        return Response(
            {
                "message": "Logged out successfully."
            }
        )


@session_list_schema
class SessionListView(generics.ListAPIView):

    serializer_class = UserSessionSerializer
    permission_classes = [
        permissions.IsAuthenticated
    ]

    def get_queryset(self):

        return UserSession.objects.filter(
            user=self.request.user
        )


@revoke_session_schema
class RevokeSessionView(
    generics.GenericAPIView
):

    permission_classes = [
        permissions.IsAuthenticated
    ]

    def post(self, request, pk):

        revoked_count = revoke_sessions(
            UserSession.objects.filter(
                id=pk,
                user=request.user,
            )
        )

        if not revoked_count:
            return Response(
                {
                    "detail": "Session not found."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        record(
            AuditEvent.Type.SESSIONS_REVOKED,
            user=request.user,
            count=revoked_count,
            session=str(pk),
        )

        return Response(
            {
                "message": "Session revoked successfully."
            }
        )


@revoke_all_sessions_schema
class RevokeAllSessionsView(
    generics.GenericAPIView
):

    permission_classes = [
        permissions.IsAuthenticated
    ]

    def post(self, request):

        revoked_count = revoke_all_sessions(
            request.user
        )

        record(
            AuditEvent.Type.SESSIONS_REVOKED,
            user=request.user,
            count=revoked_count,
            scope="all",
        )

        return Response(
            {
                "message": (
                    "All sessions revoked successfully."
                ),
                "revoked_sessions": revoked_count,
            }
        )
