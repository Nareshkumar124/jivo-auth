"""
Pages this service hosts for the links in its emails, so password reset and
email verification work without any application building its own page.
They use the same serializers and services as the API.
"""

from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from .serializers import ResetPasswordSerializer, VerifyEmailSerializer
from .services import InvalidTokenError, reset_password, verify_email


def form_errors(serializer):
    return [
        message
        for messages in serializer.errors.values()
        for message in messages
    ]


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
