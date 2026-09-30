"""OpenAPI documentation for the authentication and session endpoints."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import serializers

from config.openapi import (
    ACCESS_TOKEN_EXAMPLE,
    ACCESS_TOKEN_LIFETIME,
    AUTH_NONE,
    AUTH_REQUIRED,
    JWK_EXAMPLE,
    REFRESH_REUSE_GRACE,
    REFRESH_TOKEN_EXAMPLE,
    REFRESH_TOKEN_LIFETIME,
    SESSION_ID_EXAMPLE,
    UNAUTHORIZED_RESPONSE,
    MessageSerializer,
    error_response,
    example,
    rate_limit,
    throttled_response,
    validation_error_response,
)

from .serializers import UserSessionSerializer


class TokenPairSerializer(serializers.Serializer):

    access = serializers.CharField(
        help_text=(
            "Access token for the `Authorization: Bearer` header. "
            f"Valid for {ACCESS_TOKEN_LIFETIME}."
        ),
    )

    refresh = serializers.CharField(
        help_text=(
            "Refresh token for `POST /api/v1/auth/refresh/` and "
            f"`POST /api/v1/auth/logout/`. Valid for {REFRESH_TOKEN_LIFETIME}."
        ),
    )


class JWKSerializer(serializers.Serializer):

    kty = serializers.CharField(help_text="Key type, `RSA`.")
    use = serializers.CharField(help_text="`sig`: the key verifies signatures.")
    alg = serializers.CharField(help_text="Signing algorithm, `RS256`.")
    kid = serializers.CharField(
        help_text="Key ID. Matches the `kid` header of tokens it signed.",
    )
    n = serializers.CharField(help_text="RSA modulus, base64url.")
    e = serializers.CharField(help_text="RSA public exponent, base64url.")


class JWKSSerializer(serializers.Serializer):

    keys = JWKSerializer(
        many=True,
        help_text="Keys that verify tokens issued by this service.",
    )


class RevokeAllSessionsResponseSerializer(serializers.Serializer):

    message = serializers.CharField(
        help_text="Human-readable result of the operation.",
    )

    revoked_sessions = serializers.IntegerField(
        help_text="Number of active sessions that were revoked.",
    )


TOKEN_PAIR_EXAMPLE = example(
    "Token pair",
    {
        "access": ACCESS_TOKEN_EXAMPLE,
        "refresh": REFRESH_TOKEN_EXAMPLE,
    },
)

SESSION_EXAMPLE = {
    "id": SESSION_ID_EXAMPLE,
    "device_name": "Alice's MacBook",
    "ip_address": "203.0.113.5",
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5)",
    "created_at": "2026-09-30T06:07:16.527768Z",
    "last_used_at": "2026-09-30T08:12:41.103920Z",
    "revoked_at": None,
    "is_active": True,
}


login_schema = extend_schema(
    tags=["Authentication"],
    summary="Log in",
    description=f"""
Authenticate with email and password.

Returns an access token (valid for {ACCESS_TOKEN_LIFETIME}) and a refresh
token (valid for {REFRESH_TOKEN_LIFETIME}), and records a session for the
device, which can later be listed and revoked under **Sessions**. Email
matching is case-insensitive.

Logging in succeeds for any active account with a verified email address.
Whether the user may use a given application is in the token's `apps`
claim, which that application checks.

An application that logs users in from its own server can send its API key
in `X-Jivo-App-Key` and the user's IP address in `X-Jivo-Client-IP`; the
rate limit and the session record then use the user's IP instead of the
server's.

Rate limited to {rate_limit("login")} per account and client, and
{rate_limit("login_ip")} per client.

{AUTH_NONE}
""",
    auth=[],
    examples=[
        OpenApiExample(
            "Login",
            value={
                "email": "alice@example.com",
                "password": "Str0ng!Pass",
                "device_name": "Alice's MacBook",
            },
            request_only=True,
        ),
    ],
    responses={
        200: OpenApiResponse(
            TokenPairSerializer,
            description="Logged in.",
            examples=[TOKEN_PAIR_EXAMPLE],
        ),
        400: validation_error_response(
            "A required field is missing or invalid.",
            example(
                "Missing fields",
                {
                    "email": ["This field is required."],
                    "password": ["This field is required."],
                },
            ),
        ),
        401: error_response(
            (
                "Wrong email or password, the account is inactive, or the "
                "email address is not verified yet (`code` "
                "`email_not_verified`; see "
                "`POST /api/v1/users/resend-verification/`)."
            ),
            example(
                "Invalid credentials",
                {
                    "detail": (
                        "No active account found with the given credentials"
                    )
                },
            ),
            example(
                "Email not verified",
                {
                    "detail": "Email address is not verified.",
                    "code": "email_not_verified",
                },
            ),
        ),
        429: throttled_response("login"),
    },
)


refresh_schema = extend_schema(
    tags=["Authentication"],
    summary="Refresh tokens",
    description=f"""
Exchange a refresh token for a new access token.

Refresh tokens **rotate**: the response contains a new refresh token and the
one sent is blacklisted, so clients must store the new one. The session's
last-used time is updated.

Both new tokens carry the user's current claims, so changes to the user's
email or application access (`apps`) take effect on the next refresh.

For {REFRESH_REUSE_GRACE} after a refresh, the refresh token sent is
accepted once more and returns the same new tokens, so a lost response or
two requests racing don't log the user out. Sending it again after that
revokes the whole session, because only a copy of the token can still be
using it.

Fails with `401` if the refresh token is malformed, expired, already used,
or belongs to a session that was logged out or revoked, or if the account
has been deactivated.

{AUTH_NONE} The refresh token goes in the request body.
""",
    auth=[],
    examples=[
        OpenApiExample(
            "Refresh",
            value={
                "refresh": REFRESH_TOKEN_EXAMPLE,
            },
            request_only=True,
        ),
    ],
    responses={
        200: OpenApiResponse(
            TokenPairSerializer,
            description="New access token and rotated refresh token.",
            examples=[TOKEN_PAIR_EXAMPLE],
        ),
        400: validation_error_response(
            "The `refresh` field is missing.",
            example(
                "Missing token",
                {
                    "refresh": ["This field is required."],
                },
            ),
        ),
        401: error_response(
            "The refresh token is invalid, expired, blacklisted or revoked.",
            example(
                "Already used or revoked",
                {
                    "detail": "Token is blacklisted",
                    "code": "token_not_valid",
                },
            ),
            example(
                "Malformed",
                {
                    "detail": "Token is invalid",
                    "code": "token_not_valid",
                },
            ),
        ),
    },
)


verify_schema = extend_schema(
    tags=["Authentication"],
    summary="Verify a token",
    description=f"""
Check whether an access or refresh token is valid: signature, expiry and,
for refresh tokens, the blacklist.

Intended for clients that cannot verify JWTs themselves. Services should
verify access tokens locally instead, with the public key from
`GET /.well-known/jwks.json`.

{AUTH_NONE} The token to check goes in the request body.
""",
    auth=[],
    examples=[
        OpenApiExample(
            "Verify",
            value={
                "token": ACCESS_TOKEN_EXAMPLE,
            },
            request_only=True,
        ),
    ],
    responses={
        200: OpenApiResponse(
            OpenApiTypes.OBJECT,
            description="The token is valid. The body is an empty object.",
            examples=[example("Valid", {})],
        ),
        400: validation_error_response(
            (
                "The `token` field is missing, or the token is a refresh "
                "token that has been blacklisted."
            ),
            example(
                "Blacklisted",
                {
                    "non_field_errors": ["Token is blacklisted"],
                },
            ),
        ),
        401: error_response(
            "The token is malformed, has an invalid signature or has expired.",
            example(
                "Invalid",
                {
                    "detail": "Token is invalid",
                    "code": "token_not_valid",
                },
            ),
        ),
    },
)


logout_schema = extend_schema(
    tags=["Authentication"],
    summary="Log out",
    description=f"""
End the current session: the given refresh token is blacklisted and its
session is marked revoked.

The access token stays valid until it expires (at most
{ACCESS_TOKEN_LIFETIME}), so clients should discard it. To log out other
devices, use **Sessions**.

{AUTH_NONE} The refresh token in the body identifies the session, so
logging out works after the access token has expired. An `Authorization`
header is ignored.
""",
    auth=[],
    examples=[
        OpenApiExample(
            "Logout",
            value={
                "refresh": REFRESH_TOKEN_EXAMPLE,
            },
            request_only=True,
        ),
    ],
    responses={
        200: OpenApiResponse(
            MessageSerializer,
            description="Logged out.",
            examples=[
                example(
                    "Logged out",
                    {
                        "message": "Logged out successfully.",
                    },
                ),
            ],
        ),
        400: validation_error_response(
            "The refresh token is missing, invalid, expired or already revoked.",
            example(
                "Invalid refresh token",
                {
                    "refresh": ["Invalid refresh token."],
                },
            ),
        ),
    },
)


jwks_schema = extend_schema(
    tags=["Authentication"],
    summary="Get the token signing keys",
    description=f"""
The public keys that verify tokens issued by this service, as a JSON Web
Key Set. Applications verify access tokens locally with the key whose
`kid` matches the token's `kid` header.

Keys change only when the signing key is rotated. Cache the set and fetch
it again when a token names an unknown `kid`. Responses allow caching for
five minutes.

When the service signs with HS256 (shared secret), the set is empty.

{AUTH_NONE}
""",
    auth=[],
    responses={
        200: OpenApiResponse(
            JWKSSerializer,
            description="The key set.",
            examples=[
                example(
                    "Key set",
                    {
                        "keys": [JWK_EXAMPLE],
                    },
                ),
            ],
        ),
    },
)


session_list_schema = extend_schema(
    tags=["Sessions"],
    summary="List sessions",
    description=f"""
List all sessions of the current user, one per login, including revoked
ones. Most recently used first. `is_active` is `false` once a session has
been logged out or revoked.

{AUTH_REQUIRED}
""",
    responses={
        200: OpenApiResponse(
            UserSessionSerializer(many=True),
            description="The user's sessions.",
            # drf-spectacular wraps examples of list endpoints in a list.
            examples=[
                example(
                    "Sessions",
                    SESSION_EXAMPLE,
                ),
            ],
        ),
        401: UNAUTHORIZED_RESPONSE,
    },
)


revoke_session_schema = extend_schema(
    tags=["Sessions"],
    summary="Revoke a session",
    description=f"""
Log out one device: the session's refresh token is blacklisted and the
session is marked revoked. Its access tokens stay valid until they expire.

{AUTH_REQUIRED}
""",
    parameters=[
        OpenApiParameter(
            "id",
            OpenApiTypes.UUID,
            OpenApiParameter.PATH,
            description="Session ID, as returned by `GET /api/v1/auth/sessions/`.",
        ),
    ],
    request=None,
    responses={
        200: OpenApiResponse(
            MessageSerializer,
            description="Session revoked.",
            examples=[
                example(
                    "Revoked",
                    {
                        "message": "Session revoked successfully.",
                    },
                ),
            ],
        ),
        401: UNAUTHORIZED_RESPONSE,
        404: error_response(
            (
                "No active session with this ID for the current user: it "
                "doesn't exist, belongs to another user, or is already revoked."
            ),
            example(
                "Not found",
                {
                    "detail": "Session not found.",
                },
            ),
        ),
    },
)


revoke_all_sessions_schema = extend_schema(
    tags=["Sessions"],
    summary="Revoke all sessions",
    description=f"""
Log out every device of the current user, including the one making the
request. Access tokens already issued stay valid until they expire.

{AUTH_REQUIRED}
""",
    request=None,
    responses={
        200: OpenApiResponse(
            RevokeAllSessionsResponseSerializer,
            description="All active sessions revoked.",
            examples=[
                example(
                    "Revoked",
                    {
                        "message": "All sessions revoked successfully.",
                        "revoked_sessions": 2,
                    },
                ),
            ],
        ),
        401: UNAUTHORIZED_RESPONSE,
    },
)
