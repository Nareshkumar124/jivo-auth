"""OpenAPI documentation for the server-to-server application endpoints."""

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)

from config.openapi import (
    USER_ID_EXAMPLE,
    error_response,
    example,
    rate_limit,
    validation_error_response,
)

from .serializers import MAX_USER_IDS, ApplicationUserSerializer


class ApplicationKeyScheme(OpenApiAuthenticationExtension):
    target_class = "applications.authentication.ApplicationKeyAuthentication"
    name = "appKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "X-Jivo-App-Key",
            "description": (
                "The application's API key, created with "
                "`python manage.py app_api_key <slug>`. Server-to-server "
                "only; never send it to a browser."
            ),
        }


APP_KEY_REQUIRED = (
    "**Authentication:** requires the application's API key "
    "(`X-Jivo-App-Key: <key>`)."
)

APP_UNAUTHORIZED_RESPONSE = error_response(
    "The API key is missing, unknown, or belongs to an inactive application.",
    example(
        "Missing key",
        {
            "detail": "Authentication credentials were not provided.",
        },
    ),
    example(
        "Invalid key",
        {
            "detail": "Invalid application API key.",
        },
    ),
)


application_users_schema = extend_schema(
    tags=["Applications"],
    summary="List the application's users",
    description=f"""
List the users who have access to the calling application, ordered by
email, for example to show who created a record or to fill a user picker.

Filter with one or more `id` parameters (at most {MAX_USER_IDS}), e.g.
`?id=<uuid>&id=<uuid>`. Users without access to the application are never
returned, so unknown IDs are simply left out.

Rate limited to {rate_limit("application")} per application.

{APP_KEY_REQUIRED}
""",
    parameters=[
        OpenApiParameter(
            "id",
            OpenApiTypes.UUID,
            OpenApiParameter.QUERY,
            many=True,
            explode=True,
            description="Only return these users. Repeat for several.",
        ),
    ],
    responses={
        200: OpenApiResponse(
            ApplicationUserSerializer(many=True),
            description="Users with access to the application.",
            # drf-spectacular wraps examples of list endpoints in a list.
            examples=[
                example(
                    "Users",
                    {
                        "id": USER_ID_EXAMPLE,
                        "email": "alice@jivo.in",
                        "first_name": "Alice",
                        "last_name": "Smith",
                        "employee_code": "JIVO1234",
                        "is_active": True,
                    },
                ),
            ],
        ),
        400: validation_error_response(
            f"An `id` is not a UUID, or more than {MAX_USER_IDS} were sent.",
            example(
                "Invalid ID",
                {
                    "id": ["Each ID must be a UUID."],
                },
            ),
        ),
        401: APP_UNAUTHORIZED_RESPONSE,
    },
)
