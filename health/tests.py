from io import StringIO

import jsonschema
from django.core.management import call_command
from django.urls import reverse
from drf_spectacular.drainage import reset_generator_stats
from drf_spectacular.generators import SchemaGenerator
from rest_framework import status
from rest_framework.test import APITestCase


PUBLIC_OPERATIONS = {
    ("POST", "/api/v1/auth/login/"),
    ("POST", "/api/v1/auth/refresh/"),
    ("POST", "/api/v1/auth/verify/"),
    ("POST", "/api/v1/auth/logout/"),
    ("POST", "/api/v1/users/register/"),
    ("POST", "/api/v1/users/forgot-password/"),
    ("POST", "/api/v1/users/reset-password/"),
    ("POST", "/api/v1/users/verify-email/"),
    ("POST", "/api/v1/users/resend-verification/"),
    ("GET", "/api/v1/health/"),
    ("GET", "/.well-known/jwks.json"),
}

# Authenticated with an application API key instead of an access token.
APPLICATION_OPERATIONS = {
    ("GET", "/api/v1/apps/users/"),
}


class HealthCheckTests(APITestCase):

    def test_health_check_is_public(self):
        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            {
                "status": "ok",
                "service": "auth-service",
            },
        )


class APIDocsTests(APITestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.spec = SchemaGenerator().get_schema(request=None, public=True)

    def operations(self):
        for path, methods in self.spec["paths"].items():
            for method, operation in methods.items():
                yield method.upper(), path, operation

    def to_json_schema(self, node):
        # OpenAPI 3.0 -> JSON Schema: inline $refs and turn `nullable` into
        # a null type option.
        if isinstance(node, list):
            return [self.to_json_schema(item) for item in node]

        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            name = node["$ref"].split("/")[-1]

            return self.to_json_schema(
                self.spec["components"]["schemas"][name]
            )

        schema = {
            key: self.to_json_schema(value)
            for key, value in node.items()
            if key not in ("nullable", "readOnly", "writeOnly")
        }

        if node.get("nullable"):
            schema = {"anyOf": [schema, {"type": "null"}]}

        return schema

    def test_openapi_schema_is_served(self):
        response = self.client.get(reverse("schema"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_swagger_ui_is_served(self):
        response = self.client.get(reverse("swagger-ui"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_openapi_schema_is_valid_and_complete(self):
        reset_generator_stats()

        # Raises if any view can't be documented or the schema is invalid.
        call_command(
            "spectacular",
            "--validate",
            "--fail-on-warn",
            stdout=StringIO(),
            stderr=StringIO(),
        )

    def test_every_operation_is_documented(self):
        for method, path, operation in self.operations():
            with self.subTest(operation=f"{method} {path}"):
                self.assertTrue(operation.get("tags"))
                self.assertTrue(operation.get("summary"))
                self.assertTrue(operation.get("description"))

    def test_security_matches_authentication_requirements(self):
        self.assertNotIn("security", self.spec)

        for method, path, operation in self.operations():
            with self.subTest(operation=f"{method} {path}"):
                if (method, path) in PUBLIC_OPERATIONS:
                    self.assertNotIn("security", operation)
                elif (method, path) in APPLICATION_OPERATIONS:
                    self.assertEqual(
                        operation.get("security"),
                        [{"appKey": []}],
                    )
                    self.assertIn("401", operation["responses"])
                else:
                    self.assertEqual(
                        operation.get("security"),
                        [{"jwtAuth": []}],
                    )
                    self.assertIn("401", operation["responses"])

        for scheme in ("jwtAuth", "appKey"):
            self.assertIn(
                scheme,
                self.spec["components"]["securitySchemes"],
            )

    def test_examples_match_their_schemas(self):
        for method, path, operation in self.operations():
            bodies = []

            request_body = operation.get("requestBody")
            if request_body:
                bodies.append(("request", request_body))

            bodies.extend(operation["responses"].items())

            for where, body in bodies:
                content = body.get("content", {}).get("application/json")
                if not content:
                    continue

                schema = self.to_json_schema(content["schema"])

                for name, example in content.get("examples", {}).items():
                    with self.subTest(
                        operation=f"{method} {path}",
                        body=where,
                        example=name,
                    ):
                        jsonschema.validate(example["value"], schema)
