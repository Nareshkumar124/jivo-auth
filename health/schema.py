"""OpenAPI documentation for the health endpoint."""

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers

from config.openapi import AUTH_NONE, example


class HealthCheckResponseSerializer(serializers.Serializer):

    status = serializers.CharField(
        help_text="Always `ok` when the service is up.",
    )

    service = serializers.CharField(
        help_text="Service name.",
    )


health_check_schema = extend_schema(
    tags=["Health"],
    summary="Health check",
    description=f"""
Liveness probe. Returns `200` while the service is running. It doesn't
check the database.

{AUTH_NONE}
""",
    auth=[],
    responses={
        200: OpenApiResponse(
            HealthCheckResponseSerializer,
            description="The service is up.",
            examples=[
                example(
                    "Healthy",
                    {
                        "status": "ok",
                        "service": "auth-service",
                    },
                ),
            ],
        ),
    },
)
