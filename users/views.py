from django.conf import settings
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework import status

from .models import User
from .schema import (
    change_password_schema,
    current_user_schema,
    forgot_password_schema,
    register_schema,
    resend_verification_schema,
    reset_password_schema,
    verify_email_schema,
)

from .serializers import (
    RegisterSerializer,
    UserSerializer,
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    ResendVerificationSerializer,
    ResetPasswordSerializer,
    VerifyEmailSerializer,
)
from .services import (
    InvalidTokenError,
    reset_password,
    send_email_verification,
    send_password_reset,
    set_new_password,
    verify_email,
)


class RegistrationOpen(permissions.BasePermission):

    message = "Registration is disabled."

    def has_permission(self, request, view):
        return settings.REGISTRATION_ENABLED


@register_schema
class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = RegisterSerializer

    # No authenticators, so a closed registration answers 403, not 401.
    authentication_classes = []

    permission_classes = [
        RegistrationOpen,
    ]
    throttle_scope = "register"

    def perform_create(self, serializer):
        user = serializer.save()

        send_email_verification(user)


@current_user_schema
class CurrentUserView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [
        permissions.IsAuthenticated,
    ]
    http_method_names = [
        "get",
        "patch",
        "head",
        "options",
    ]

    def get_object(self):
        return self.request.user


@change_password_schema
class ChangePasswordView(generics.GenericAPIView):

    serializer_class = ChangePasswordSerializer
    permission_classes = [
        permissions.IsAuthenticated,
    ]

    def post(self, request):

        serializer = self.get_serializer(
            data=request.data
        )

        serializer.is_valid(raise_exception=True)

        set_new_password(
            request.user,
            serializer.validated_data["new_password"],
        )

        return Response(
            {
                "message": (
                    "Password changed successfully. "
                    "Please log in again."
                )
            },
            status=status.HTTP_200_OK,
        )


@forgot_password_schema
class ForgotPasswordView(generics.GenericAPIView):

    serializer_class = ForgotPasswordSerializer
    permission_classes = [
        permissions.AllowAny,
    ]
    throttle_scope = "forgot_password"

    def post(self, request):

        serializer = self.get_serializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        email = serializer.validated_data[
            "email"
        ]

        user = User.objects.filter(
            email__iexact=email,
            is_active=True,
        ).first()

        # Same response whether or not the account exists.
        response_data = {
            "message": (
                "If the account exists, "
                "a password reset email "
                "has been sent."
            )
        }

        if not user:
            return Response(response_data)

        raw_token = send_password_reset(user)

        # Local development only; see RETURN_RESET_TOKEN_IN_RESPONSE.
        if settings.RETURN_RESET_TOKEN_IN_RESPONSE:
            response_data["reset_token"] = raw_token

        return Response(response_data)


@reset_password_schema
class ResetPasswordView(generics.GenericAPIView):

    serializer_class = ResetPasswordSerializer
    permission_classes = [
        permissions.AllowAny,
    ]
    throttle_scope = "reset_password"

    def post(self, request):

        serializer = self.get_serializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        try:
            reset_password(
                serializer.validated_data["token"],
                serializer.validated_data["new_password"],
            )
        except InvalidTokenError as exc:
            return Response(
                {
                    "detail": str(exc)
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "message": (
                    "Password reset successfully."
                )
            }
        )


@verify_email_schema
class VerifyEmailView(generics.GenericAPIView):

    serializer_class = VerifyEmailSerializer
    permission_classes = [
        permissions.AllowAny,
    ]
    throttle_scope = "verify_email"

    def post(self, request):

        serializer = self.get_serializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        try:
            verify_email(
                serializer.validated_data["token"]
            )
        except InvalidTokenError as exc:
            return Response(
                {
                    "detail": str(exc)
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "message": "Email verified successfully."
            }
        )


@resend_verification_schema
class ResendVerificationView(generics.GenericAPIView):

    # Public, because unverified users can't log in to ask for it.
    serializer_class = ResendVerificationSerializer
    permission_classes = [
        permissions.AllowAny,
    ]
    throttle_scope = "resend_verification"

    def post(self, request):

        serializer = self.get_serializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        user = User.objects.filter(
            email__iexact=serializer.validated_data["email"],
            is_active=True,
            is_verified=False,
        ).first()

        if user:
            send_email_verification(user)

        # Same response whether or not the account exists.
        return Response(
            {
                "message": (
                    "If the account exists and is not verified yet, "
                    "a verification email has been sent."
                )
            }
        )
