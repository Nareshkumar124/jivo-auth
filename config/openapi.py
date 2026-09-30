"""
Shared drf-spectacular building blocks used by each app's ``schema.py``:
common response bodies, error responses, examples and wording.

The ``jwtAuth`` security scheme and the ``ValidationError`` component are
declared in ``SPECTACULAR_SETTINGS["APPEND_COMPONENTS"]``.
"""

from django.conf import settings
from drf_spectacular.utils import OpenApiExample, OpenApiResponse
from rest_framework import serializers


AUTH_REQUIRED = (
    "**Authentication:** requires an access token "
    "(`Authorization: Bearer <access token>`)."
)

AUTH_NONE = "**Authentication:** none."

USER_ID_EXAMPLE = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
SESSION_ID_EXAMPLE = "7c9e6679-7425-40de-944b-e07fc1f90ae7"

# Real header and claim layout, placeholder signature.
ACCESS_TOKEN_EXAMPLE = (
    "eyJhbGciOiJSUzI1NiIsImtpZCI6IklLelc3a1VhbkFPcHhQNjhTMHVpWkFWbUtZb2lW"
    "ODlPRXZQdFBSdFlsWG8iLCJ0eXAiOiJKV1QifQ."
    "eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxNzkwNzQ5MzM2LCJpYXQiOjE3OTA3"
    "NDg0MzYsImp0aSI6ImVlNjhmNmJhMTdiMDQ5MmY4NmE0NTRmMGYyMzM1MWJmIiwic3Vi"
    "IjoiM2ZhODVmNjQtNTcxNy00NTYyLWIzZmMtMmM5NjNmNjZhZmE2IiwiZW1haWwiOiJh"
    "bGljZUBqaXZvLmluIiwiYXBwcyI6WyJvbXMiXSwiaXNzIjoiaHR0cHM6Ly9hdXRoLmpp"
    "dm8uaW4ifQ.<signature>"
)

REFRESH_TOKEN_EXAMPLE = (
    "eyJhbGciOiJSUzI1NiIsImtpZCI6IklLelc3a1VhbkFPcHhQNjhTMHVpWkFWbUtZb2lW"
    "ODlPRXZQdFBSdFlsWG8iLCJ0eXAiOiJKV1QifQ."
    "eyJ0b2tlbl90eXBlIjoicmVmcmVzaCIsImV4cCI6MTc5MzM0MDQzNiwiaWF0IjoxNzkw"
    "NzQ4NDM2LCJqdGkiOiI2ZTJlOWExNzc0YTQ0NWIyODNmMjYyYTVkODAzZDFhMiIsInN1"
    "YiI6IjNmYTg1ZjY0LTU3MTctNDU2Mi1iM2ZjLTJjOTYzZjY2YWZhNiIsImVtYWlsIjoi"
    "YWxpY2VAaml2by5pbiIsImFwcHMiOlsib21zIl0sImlzcyI6Imh0dHBzOi8vYXV0aC5q"
    "aXZvLmluIn0.<signature>"
)

JWK_EXAMPLE = {
    "kty": "RSA",
    "n": (
        "uUAxT-LtTZux2MAQ9_R5CViq8wGxIqcXucJO85ZyXSBmoklyp8dYEgpA79ziCXdwS0Q"
        "w4FlPB4sxQc1hg-1-j0Lmz_gZ1oqMg1Ewcw69o-NQGnaucXwNZVPe4eNzSMLjbu2HHp"
        "pAJDObfSN2BFkj07xPyGrxOW_k3zT44CmBGctaANQeuZhcukjVqx1p9DIEpxEcL-FvT"
        "ulGFTZP---Qc6h8wOFlR6p_5NrdXvBMO_BXBzig42BlGu2AqtRx1hzkY85ctXkYcE_5"
        "Xk7UKTOimBxFX7DaGURFXxkWQPeJkBc0iEg9Ub06XpiYQiGwjZ1UlKPmh4JGv9xEpyL"
        "wUMo2Kw"
    ),
    "e": "AQAB",
    "kid": "IKzW7kUanAOpxP68S0uiZAVmKYoiV89OEvPtPRtYlXo",
    "use": "sig",
    "alg": "RS256",
}


def _plural(count, unit):
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def describe_duration(delta):
    minutes = int(delta.total_seconds() // 60)

    if minutes % (60 * 24) == 0:
        return _plural(minutes // (60 * 24), "day")

    if minutes % 60 == 0:
        return _plural(minutes // 60, "hour")

    return _plural(minutes, "minute")


ACCESS_TOKEN_LIFETIME = describe_duration(
    settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"]
)

REFRESH_TOKEN_LIFETIME = describe_duration(
    settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]
)

REFRESH_REUSE_GRACE = _plural(
    int(settings.REFRESH_TOKEN_REUSE_GRACE.total_seconds()),
    "second",
)


def rate_limit(scope):
    """Describe a DEFAULT_THROTTLE_RATES entry, e.g. "5 requests per minute"."""

    count, period = settings.REST_FRAMEWORK[
        "DEFAULT_THROTTLE_RATES"
    ][scope].split("/")

    # DRF only looks at the first letter of the period.
    unit = {
        "s": "second",
        "m": "minute",
        "h": "hour",
        "d": "day",
    }[period[0]]

    return f"{_plural(int(count), 'request')} per {unit}"


class MessageSerializer(serializers.Serializer):
    """Response body of endpoints that only return a status message."""

    message = serializers.CharField(
        help_text="Human-readable result of the operation.",
    )


class ErrorSerializer(serializers.Serializer):
    """Error body of 401, 404 and 429 responses, and of some 400 responses."""

    detail = serializers.CharField(
        help_text="Human-readable error message.",
    )

    code = serializers.CharField(
        required=False,
        help_text=(
            "Machine-readable error code. Present on token errors, "
            "e.g. `token_not_valid`."
        ),
    )

    messages = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        help_text=(
            "Per token type details. Present when an access token "
            "sent in the `Authorization` header is rejected."
        ),
    )


def example(name, value, description=""):
    return OpenApiExample(
        name,
        value=value,
        description=description,
    )


def error_response(description, *examples):
    return OpenApiResponse(
        response=ErrorSerializer,
        description=description,
        examples=list(examples),
    )


def validation_error_response(description, *examples):
    return OpenApiResponse(
        response={"$ref": "#/components/schemas/ValidationError"},
        description=description,
        examples=list(examples),
    )


def throttled_response(scope):
    return error_response(
        (
            f"Rate limit exceeded ({rate_limit(scope)} per client). "
            "Retry after the number of seconds in the `Retry-After` header."
        ),
        example(
            "Throttled",
            {
                "detail": (
                    "Request was throttled. "
                    "Expected available in 60 seconds."
                )
            },
        ),
    )


UNAUTHORIZED_RESPONSE = error_response(
    "The access token is missing, malformed, expired or not an access token.",
    example(
        "Missing token",
        {
            "detail": "Authentication credentials were not provided.",
        },
    ),
    example(
        "Invalid token",
        {
            "detail": "Given token not valid for any token type",
            "code": "token_not_valid",
            "messages": [
                {
                    "token_class": "AccessToken",
                    "token_type": "access",
                    "message": "Token is invalid",
                }
            ],
        },
    ),
)
