"""
Pages this service hosts, so password reset and email verification work
without any application building its own: the forgot-password form that
applications link to, and the pages the emailed links open. They use the
same serializers, services and rate limits as the API.
"""

from types import SimpleNamespace

from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from applications.throttling import ScopedRateThrottle

from .models import User
from .serializers import (
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
    verify_email,
)


def throttled(request, scope):
    """Whether the request exceeds the API's rate for `scope` (shared)."""

    return not ScopedRateThrottle().allow_request(
        request,
        SimpleNamespace(throttle_scope=scope),
    )


def form_errors(serializer):
    return [
        message
        for messages in serializer.errors.values()
        for message in messages
    ]


@method_decorator(never_cache, name="dispatch")
class ForgotPasswordPage(View):
    """Linked from applications' login pages ("Forgot your password?")."""

    template_name = "users/forgot_password.html"

    def get(self, request):
        return render(request, self.template_name)

    def post(self, request):

        if throttled(request, "forgot_password"):
            return render(
                request,
                self.template_name,
                {
                    "errors": ["Too many requests. Try again in a minute."],
                },
                status=429,
            )

        serializer = ForgotPasswordSerializer(
            data=request.POST,
        )

        if not serializer.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "errors": form_errors(serializer),
                },
                status=400,
            )

        user = User.objects.filter(
            email__iexact=serializer.validated_data["email"],
            is_active=True,
        ).first()

        if user:
            send_password_reset(user)

        # Same page whether or not the account exists.
        return render(
            request,
            self.template_name,
            {
                "done": True,
            },
        )


@method_decorator(never_cache, name="dispatch")
class ResetPasswordPage(View):

    template_name = "users/reset_password.html"

    def get(self, request):
        return render(
            request,
            self.template_name,
            {
                "token": request.GET.get("token", ""),
            },
        )

    def post(self, request):

        token = request.POST.get("token", "")

        serializer = ResetPasswordSerializer(
            data=request.POST,
        )

        if not serializer.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "token": token,
                    "errors": form_errors(serializer),
                },
                status=400,
            )

        try:
            reset_password(
                serializer.validated_data["token"],
                serializer.validated_data["new_password"],
            )
        except InvalidTokenError as exc:
            return render(
                request,
                self.template_name,
                {
                    "token": token,
                    "token_error": str(exc),
                },
                status=400,
            )

        return render(
            request,
            self.template_name,
            {
                "done": True,
            },
        )


@method_decorator(never_cache, name="dispatch")
class VerifyEmailPage(View):

    # Verifying takes a POST, so link scanners that fetch the page don't
    # verify the address on the user's behalf.
    template_name = "users/verify_email.html"

    def get(self, request):
        return render(
            request,
            self.template_name,
            {
                "token": request.GET.get("token", ""),
            },
        )

    def post(self, request):

        # The "send me a new link" form on the invalid-link page.
        if "email" in request.POST:
            return self.resend(request)

        serializer = VerifyEmailSerializer(
            data=request.POST,
        )

        if not serializer.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "token_error": "The verification link is incomplete.",
                },
                status=400,
            )

        try:
            verify_email(
                serializer.validated_data["token"],
            )
        except InvalidTokenError as exc:
            return render(
                request,
                self.template_name,
                {
                    "token_error": str(exc),
                },
                status=400,
            )

        return render(
            request,
            self.template_name,
            {
                "done": True,
            },
        )

    def resend(self, request):

        if throttled(request, "resend_verification"):
            return render(
                request,
                self.template_name,
                {
                    "token_error": "Too many requests. Try again in a minute.",
                },
                status=429,
            )

        serializer = ResendVerificationSerializer(
            data=request.POST,
        )

        if not serializer.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "token_error": "Enter a valid email address.",
                },
                status=400,
            )

        user = User.objects.filter(
            email__iexact=serializer.validated_data["email"],
            is_active=True,
            is_verified=False,
        ).first()

        if user:
            send_email_verification(user)

        # Same page whether or not the account exists.
        return render(
            request,
            self.template_name,
            {
                "resent": True,
            },
        )
