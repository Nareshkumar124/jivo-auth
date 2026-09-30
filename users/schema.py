"""OpenAPI documentation for the user and password endpoints."""

from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import serializers

from config.openapi import (
    AUTH_NONE,
    AUTH_REQUIRED,
    UNAUTHORIZED_RESPONSE,
    USER_ID_EXAMPLE,
    MessageSerializer,
    error_response,
    example,
    rate_limit,
    throttled_response,
    validation_error_response,
)

from .serializers import RegisterSerializer, UserSerializer


PASSWORD_RULES = """
Password rules: at least 8 characters with an uppercase letter, a lowercase
letter, a digit and a special character. Common passwords and passwords too
similar to the user's email or name are rejected.
"""

LOGS_OUT_EVERYWHERE = """
On success, every session of the user is revoked (all devices, including
the current one) and any pending password reset tokens stop working. The
user must log in again with the new password. Access tokens already issued
stay valid until they expire.
"""


class ForgotPasswordResponseSerializer(serializers.Serializer):

    message = serializers.CharField(
        help_text="Same message whether or not the account exists.",
    )

    reset_token = serializers.CharField(
        required=False,
        help_text=(
            "The emailed reset token. Only returned by a local development "
            "server (`DEBUG` on and emails printed to the console)."
        ),
    )


USER_EXAMPLE = {
    "id": USER_ID_EXAMPLE,
    "email": "alice@example.com",
    "first_name": "Alice",
    "last_name": "Smith",
    "is_active": True,
    "is_verified": False,
    "apps": ["oms"],
    "created_at": "2026-09-30T06:07:16.357563Z",
    "updated_at": "2026-09-30T06:07:16.399186Z",
}

FORGOT_PASSWORD_MESSAGE = (
    "If the account exists, a password reset email has been sent."
)


register_schema = extend_schema(
    tags=["Users"],
    summary="Register",
    description=f"""
Create a user account and email the user a link to verify the address
(`POST /api/v1/users/verify-email/`).

The email is stored lowercased and must be unique regardless of case. The
server can limit registration to some email domains, or turn it off.
{PASSWORD_RULES}
Registering does not log the user in. Unless the server allows it, login
is refused until the email address is verified. A new account has access to
no application until an administrator grants it.

Rate limited to {rate_limit("register")} per client.

{AUTH_NONE}
""",
    auth=[],
    examples=[
        OpenApiExample(
            "Register",
            value={
                "email": "alice@example.com",
                "password": "Str0ng!Pass",
                "password_confirm": "Str0ng!Pass",
                "first_name": "Alice",
                "last_name": "Smith",
            },
            request_only=True,
        ),
    ],
    responses={
        201: OpenApiResponse(
            RegisterSerializer,
            description="Account created.",
            examples=[
                example(
                    "Created",
                    {
                        "email": "alice@example.com",
                        "first_name": "Alice",
                        "last_name": "Smith",
                    },
                ),
            ],
        ),
        400: validation_error_response(
            (
                "Invalid input: missing or malformed fields, email already "
                "registered, passwords don't match, or password too weak."
            ),
            example(
                "Email taken",
                {
                    "email": ["user with this email already exists."],
                },
            ),
            example(
                "Weak password",
                {
                    "password": ["This password is too common."],
                },
            ),
            example(
                "Passwords don't match",
                {
                    "password": ["Passwords do not match."],
                },
            ),
            example(
                "Domain not allowed",
                {
                    "email": [
                        "Registration is limited to @jivo.in email addresses."
                    ],
                },
            ),
        ),
        403: error_response(
            "Registration is turned off on this server.",
            example(
                "Disabled",
                {
                    "detail": "Registration is disabled.",
                },
            ),
        ),
        429: throttled_response("register"),
    },
)


current_user_schema = extend_schema_view(
    get=extend_schema(
        tags=["Users"],
        summary="Get current user",
        description=f"""
Return the profile of the authenticated user.

{AUTH_REQUIRED}
""",
        responses={
            200: OpenApiResponse(
                UserSerializer,
                description="The current user.",
                examples=[example("User", USER_EXAMPLE)],
            ),
            401: UNAUTHORIZED_RESPONSE,
        },
    ),
    patch=extend_schema(
        tags=["Users"],
        summary="Update current user",
        description=f"""
Update the first and/or last name of the authenticated user. Send only the
fields to change.

`id`, `email`, `is_active`, `is_verified`, `apps` and the timestamps are
read-only and ignored if sent.

{AUTH_REQUIRED}
""",
        examples=[
            OpenApiExample(
                "Rename",
                value={
                    "first_name": "Alicia",
                },
                request_only=True,
            ),
        ],
        responses={
            200: OpenApiResponse(
                UserSerializer,
                description="The updated user.",
                examples=[
                    example(
                        "Updated",
                        {
                            **USER_EXAMPLE,
                            "first_name": "Alicia",
                            "updated_at": "2026-09-30T06:07:16.533631Z",
                        },
                    ),
                ],
            ),
            400: validation_error_response(
                "A field value is invalid, e.g. longer than 100 characters.",
                example(
                    "Too long",
                    {
                        "first_name": [
                            "Ensure this field has no more than 100 characters."
                        ],
                    },
                ),
            ),
            401: UNAUTHORIZED_RESPONSE,
        },
    ),
)


change_password_schema = extend_schema(
    tags=["Password"],
    summary="Change password",
    description=f"""
Change the password of the authenticated user. Requires the current
password.
{PASSWORD_RULES}{LOGS_OUT_EVERYWHERE}
{AUTH_REQUIRED}
""",
    examples=[
        OpenApiExample(
            "Change password",
            value={
                "current_password": "Str0ng!Pass",
                "new_password": "N3w!Password",
                "new_password_confirm": "N3w!Password",
            },
            request_only=True,
        ),
    ],
    responses={
        200: OpenApiResponse(
            MessageSerializer,
            description="Password changed; all sessions revoked.",
            examples=[
                example(
                    "Changed",
                    {
                        "message": (
                            "Password changed successfully. "
                            "Please log in again."
                        ),
                    },
                ),
            ],
        ),
        400: validation_error_response(
            (
                "The current password is wrong, the new passwords don't "
                "match, or the new password is too weak or unchanged."
            ),
            example(
                "Wrong current password",
                {
                    "current_password": ["Current password is incorrect."],
                },
            ),
            example(
                "Unchanged",
                {
                    "new_password": [
                        "New password must be different from the "
                        "current password."
                    ],
                },
            ),
        ),
        401: UNAUTHORIZED_RESPONSE,
    },
)


forgot_password_schema = extend_schema(
    tags=["Password"],
    summary="Request a password reset",
    description=f"""
Start a password reset. If an active account exists for the email, it is
sent a link containing a single-use reset token valid for 15 minutes. The
link opens the password reset page hosted by this service unless the server
is configured with another page; that page uses the token with
`POST /api/v1/users/reset-password/`.

The response is the same whether or not the account exists, so it can't be
used to discover accounts.

Rate limited to {rate_limit("forgot_password")} per client.

{AUTH_NONE}
""",
    auth=[],
    examples=[
        OpenApiExample(
            "Forgot password",
            value={
                "email": "alice@example.com",
            },
            request_only=True,
        ),
    ],
    responses={
        200: OpenApiResponse(
            ForgotPasswordResponseSerializer,
            description="Request accepted.",
            examples=[
                example(
                    "Accepted",
                    {
                        "message": FORGOT_PASSWORD_MESSAGE,
                    },
                ),
                example(
                    "Accepted (DEBUG)",
                    {
                        "message": FORGOT_PASSWORD_MESSAGE,
                        "reset_token": (
                            "WBRyblFCgEIGAb59D0w_UrATy69APyCPZE-"
                            "Y6-d8PatTnXwqOUebt87H9i5D4-8-"
                        ),
                    },
                    "Only from a local development server.",
                ),
            ],
        ),
        400: validation_error_response(
            "The email is missing or malformed.",
            example(
                "Invalid email",
                {
                    "email": ["Enter a valid email address."],
                },
            ),
        ),
        429: throttled_response("forgot_password"),
    },
)


reset_password_schema = extend_schema(
    tags=["Password"],
    summary="Reset password",
    description=f"""
Set a new password using the token from the password reset email. The token
is single-use and expires 15 minutes after it was requested.
{PASSWORD_RULES}{LOGS_OUT_EVERYWHERE}
Rate limited to {rate_limit("reset_password")} per client.

{AUTH_NONE}
""",
    auth=[],
    examples=[
        OpenApiExample(
            "Reset password",
            value={
                "token": (
                    "WBRyblFCgEIGAb59D0w_UrATy69APyCPZE-"
                    "Y6-d8PatTnXwqOUebt87H9i5D4-8-"
                ),
                "new_password": "N3w!Password",
                "new_password_confirm": "N3w!Password",
            },
            request_only=True,
        ),
    ],
    responses={
        200: OpenApiResponse(
            MessageSerializer,
            description="Password reset; all sessions revoked.",
            examples=[
                example(
                    "Reset",
                    {
                        "message": "Password reset successfully.",
                    },
                ),
            ],
        ),
        400: OpenApiResponse(
            response={
                "oneOf": [
                    {"$ref": "#/components/schemas/ValidationError"},
                    {"$ref": "#/components/schemas/Error"},
                ],
            },
            description=(
                "Invalid input (field errors), or the token is unknown, "
                "already used or expired (`detail`)."
            ),
            examples=[
                example(
                    "Invalid token",
                    {
                        "detail": "Invalid reset token.",
                    },
                ),
                example(
                    "Expired token",
                    {
                        "detail": "Reset token has expired.",
                    },
                ),
                example(
                    "Weak password",
                    {
                        "new_password": [
                            "Ensure this field has at least 8 characters."
                        ],
                    },
                ),
            ],
        ),
        429: throttled_response("reset_password"),
    },
)


verify_email_schema = extend_schema(
    tags=["Users"],
    summary="Verify email address",
    description=f"""
Mark the user's email address as verified (`is_verified`) using the token
from the verification email. The token expires 24 hours after it was sent,
and requesting a new email invalidates earlier tokens.

Rate limited to {rate_limit("verify_email")} per client.

{AUTH_NONE}
""",
    auth=[],
    examples=[
        OpenApiExample(
            "Verify email",
            value={
                "token": (
                    "q0h0MyShm0xFZpXcv_1q4Yl3Qp7Tq1uM2ZqK4vwJbE8"
                    "ho2cN1xU0tWmYk3sV0rDa"
                ),
            },
            request_only=True,
        ),
    ],
    responses={
        200: OpenApiResponse(
            MessageSerializer,
            description="Email verified.",
            examples=[
                example(
                    "Verified",
                    {
                        "message": "Email verified successfully.",
                    },
                ),
            ],
        ),
        400: OpenApiResponse(
            response={
                "oneOf": [
                    {"$ref": "#/components/schemas/ValidationError"},
                    {"$ref": "#/components/schemas/Error"},
                ],
            },
            description=(
                "The `token` field is missing (field errors), or the token "
                "is unknown, already used or expired (`detail`)."
            ),
            examples=[
                example(
                    "Invalid token",
                    {
                        "detail": "Invalid verification token.",
                    },
                ),
                example(
                    "Expired token",
                    {
                        "detail": "Verification token has expired.",
                    },
                ),
            ],
        ),
        429: throttled_response("verify_email"),
    },
)


resend_verification_schema = extend_schema(
    tags=["Users"],
    summary="Resend verification email",
    description=f"""
Send a new email verification link, if an active, unverified account exists
for the email. Earlier links stop working.

Public, because an unverified user can't log in. The response is the same
whether or not the account exists.

Rate limited to {rate_limit("resend_verification")} per client.

{AUTH_NONE}
""",
    auth=[],
    examples=[
        OpenApiExample(
            "Resend",
            value={
                "email": "alice@example.com",
            },
            request_only=True,
        ),
    ],
    responses={
        200: OpenApiResponse(
            MessageSerializer,
            description="Request accepted.",
            examples=[
                example(
                    "Accepted",
                    {
                        "message": (
                            "If the account exists and is not verified yet, "
                            "a verification email has been sent."
                        ),
                    },
                ),
            ],
        ),
        400: validation_error_response(
            "The email is missing or malformed.",
            example(
                "Invalid email",
                {
                    "email": ["Enter a valid email address."],
                },
            ),
        ),
        429: throttled_response("resend_verification"),
    },
)
