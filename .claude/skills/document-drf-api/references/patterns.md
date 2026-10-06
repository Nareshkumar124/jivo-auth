# drf-spectacular patterns

Copy-and-adapt building blocks for the documentation layout the skill uses.
Names such as `config/` (the Django settings package) and `jwtAuth` are
placeholders: use the project's settings package and the security scheme
names drf-spectacular actually generates for its authentication classes.

## Contents

1. [Gotchas](#1-gotchas) (read this first)
2. [SPECTACULAR_SETTINGS](#2-spectacular_settings)
3. [Shared helpers module](#3-shared-helpers-module)
4. [Per-app schema.py](#4-per-app-schemapy)
5. [ViewSets, query parameters, pagination, 403, 204](#5-viewsets-query-parameters-pagination-403-204)
6. [Applying schemas to views](#6-applying-schemas-to-views)
7. [Serializer field descriptions](#7-serializer-field-descriptions)
8. [Regression tests](#8-regression-tests)
9. [Capturing real responses](#9-capturing-real-responses)
10. [Custom authentication classes](#10-custom-authentication-classes)

---

## 1. Gotchas

| Symptom | Cause | Fix |
| --- | --- | --- |
| Public endpoints show a lock, every operation lists an extra scheme | `SPECTACULAR_SETTINGS["SECURITY"]` is **appended to every operation** | Delete `SECURITY`; security comes from each view's authentication classes |
| Operation references a scheme missing from `securitySchemes` | `COMPONENTS` is not a drf-spectacular setting, so it is silently ignored | Use `APPEND_COMPONENTS` (it overrides/extends generated components) |
| Public `AllowAny` endpoint shows `[{jwtAuth: []}, {}]` ("optional auth") | spectacular adds `{}` for `AllowAny` but keeps the auth classes | `extend_schema(auth=[])` on public operations; with no global security, an operation without `security` is public |
| "could not resolve authenticator" warning; the endpoint looks public | A custom `BaseAuthentication` subclass has no schema extension | An `OpenApiAuthenticationExtension` for it (§10) |
| Login/refresh response documented as the request body, or missing the rotated `refresh` | SimpleJWT's contrib extensions match the **exact** SimpleJWT serializer classes only; project subclasses fall back to auto-mapping | Give the operation an explicit response serializer (e.g. `TokenPair` with `access` + `refresh`) |
| Read-only fields appear in request bodies, PATCH bodies look wrong | One component is shared by request and response | `COMPONENT_SPLIT_REQUEST: True` (request components get a `Request` suffix, PATCH gets `Patched…Request`) |
| List endpoint example is nested twice (`[[{...}]]`) | spectacular wraps examples of list responses in a list itself | Give list examples as a **single item** |
| `GenericAPIView` without `serializer_class` → "unable to guess serializer" error | spectacular calls `get_serializer()` | `extend_schema(request=None or a serializer, responses=...)` |
| An `@action` that takes no body shows the ViewSet's serializer as its request | Actions inherit `serializer_class` | `extend_schema(request=None, ...)` on the action |
| Two different `inline_serializer(name="X")` → component name collision warning | Same component name, different identities | Define the serializer once (shared module) and import it |
| Query parameters read via `request.query_params` are missing | spectacular only sees filter backends / pagination | `OpenApiParameter(..., OpenApiParameter.QUERY)` |
| Adding `help_text` produced a migration | It was put on a **model** field | Put `help_text` on the serializer field (declared field or `Meta.extra_kwargs`) |
| Component named after an internal class (`SessionTokenRefreshRequest`) | Component name = serializer class name minus `Serializer` | `@extend_schema_serializer(component_name="TokenRefresh")` |
| `--fail-on-warn` passes in the shell but fails inside the test suite | `GENERATOR_STATS` accumulate across generations in one process | Call `drf_spectacular.drainage.reset_generator_stats()` first |
| DRF answers failed auth with 403 instead of 401 | The authentication class has no `authenticate_header()` | That's behaviour, not docs. Document what the API returns and report it |

Also remember:
- `extend_schema` on a **class** applies to every method of that view. Use
  `extend_schema_view(get=..., patch=...)` when methods differ, and for
  ViewSets (`list=`, `retrieve=`, `create=`, ...).
- `http_method_names` on a view is respected; don't document methods the
  view refuses.
- `OpenApiResponse(response={...})` accepts a raw schema dict, e.g.
  `{"$ref": "#/components/schemas/ValidationError"}` or a `oneOf`.
- Every throttled view can return 429, every protected view 401. Document 429
  where a strict scoped rate applies (login, signup, password reset), not
  necessarily on every endpoint. Say which in the API description.

## 2. SPECTACULAR_SETTINGS

```python
# Per-endpoint documentation lives in each app's schema.py, built from the
# helpers in config/openapi.py.
SPECTACULAR_SETTINGS = {
    "TITLE": "Example API",
    "DESCRIPTION": """
One-paragraph overview of the service.

## Authentication

1. `POST /api/v1/auth/login/` returns an **access** and a **refresh** token.
2. Send the access token as `Authorization: Bearer <access token>`. In
   Swagger UI, click **Authorize** and paste it.
3. ... refresh flow, token lifetimes, other schemes (API keys) ...

Endpoints marked with a lock require authentication; all others are public.

## Errors

* `{"detail": "..."}` for 401, 403, 404 and 429 responses.
* Validation errors (400) map each field to a list of messages, with
  `non_field_errors` for errors not tied to one field.

## Rate limits

... global anon/user rates, which endpoints have stricter ones ...
""",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v1",        # keeps tags/operationIds clean

    # Separate request/response components, so read-only fields don't show
    # up in request bodies (and write-only ones not in responses).
    "COMPONENT_SPLIT_REQUEST": True,

    "SWAGGER_UI_SETTINGS": {
        "deepLinking": True,
        "persistAuthorization": True,        # token survives page reloads
    },

    "TAGS": [
        {"name": "Authentication", "description": "Log in, refresh and verify tokens, log out."},
        {"name": "Users", "description": "Registration and profile."},
        # one entry per tag used, in the order they should appear
    ],

    # Operations get their security requirement from the view's
    # authentication and permission classes; public views set `auth=[]`.
    # Don't add "SECURITY": it is appended to every operation.
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            # Same name as the generated scheme, to add a description.
            # SimpleJWT -> "jwtAuth"; DRF TokenAuthentication -> "tokenAuth";
            # SessionAuthentication -> "cookieAuth"; BasicAuthentication -> "basicAuth".
            "jwtAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "Access token from `POST /api/v1/auth/login/`.",
            },
        },
        "schemas": {
            "ValidationError": {
                "type": "object",
                "description": (
                    "Maps each invalid field to a list of error messages. "
                    "`non_field_errors` holds errors not tied to one field."
                ),
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
    },
}
```

Don't import the helpers module from settings: it imports DRF, which needs
settings to be loaded first. That is why `ValidationError` lives here.

Before overriding a security scheme, check the generated one (name, `type`,
`scheme`, `in`/`name` for API keys) in the baseline schema and copy it,
only adding `description`. Schemes for custom authentication classes are
better described in their extension (§10).

## 3. Shared helpers module

Put it next to settings (e.g. `config/openapi.py`). Keep only what several
apps use; app-specific response serializers go in that app's `schema.py`.

```python
"""
Shared drf-spectacular building blocks used by each app's ``schema.py``.

The security scheme description and the ``ValidationError`` component are
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

# Realistic shape, obviously fake values. Never paste real tokens/secrets.
ACCESS_TOKEN_EXAMPLE = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoiYWNjZXNzIn0.<signature>"


def _plural(count, unit):
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def rate_limit(scope):
    """Describe a DEFAULT_THROTTLE_RATES entry, e.g. "5 requests per minute"."""

    count, period = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"][scope].split("/")

    # DRF only looks at the first letter of the period.
    unit = {"s": "second", "m": "minute", "h": "hour", "d": "day"}[period[0]]

    return f"{_plural(int(count), 'request')} per {unit}"


class MessageSerializer(serializers.Serializer):
    """Response body of endpoints that only return a status message."""

    message = serializers.CharField(help_text="Human-readable result of the operation.")


class ErrorSerializer(serializers.Serializer):
    """Error body of 401, 403, 404 and 429 responses."""

    detail = serializers.CharField(help_text="Human-readable error message.")

    # Only add fields the API really returns (e.g. SimpleJWT adds `code` and
    # `messages` on token errors).
    code = serializers.CharField(
        required=False,
        help_text="Machine-readable error code, e.g. `token_not_valid`.",
    )


def example(name, value, description=""):
    return OpenApiExample(name, value=value, description=description)


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
        f"Rate limit exceeded ({rate_limit(scope)} per client). "
        "Retry after the number of seconds in the `Retry-After` header.",
        example("Throttled", {"detail": "Request was throttled. Expected available in 60 seconds."}),
    )


UNAUTHORIZED_RESPONSE = error_response(
    "Authentication credentials are missing or invalid.",
    example("Missing token", {"detail": "Authentication credentials were not provided."}),
)

FORBIDDEN_RESPONSE = error_response(
    "Authenticated, but not allowed to perform this action.",
    example("Forbidden", {"detail": "You do not have permission to perform this action."}),
)

NOT_FOUND_RESPONSE = error_response(
    "No such object, or it isn't visible to the current user.",
    example("Not found", {"detail": "No Item matches the given query."}),
)
```

Derive numbers that live in settings (token lifetimes, throttle rates) from
settings in descriptions, so the docs can't drift.

## 4. Per-app schema.py

One module-level decorator per operation, named after the view
(`login_schema`, `session_list_schema`, ...). Descriptions follow the same
shape everywhere: what it does → notable behaviour/side effects → limits →
`AUTH_REQUIRED` / `AUTH_NONE` line.

```python
"""OpenAPI documentation for the <app> endpoints."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers

from config.openapi import (
    AUTH_NONE,
    AUTH_REQUIRED,
    UNAUTHORIZED_RESPONSE,
    MessageSerializer,
    error_response,
    example,
    rate_limit,
    throttled_response,
    validation_error_response,
)

from .serializers import SessionSerializer


# Doc-only serializer for a response the view builds by hand.
class TokenPairSerializer(serializers.Serializer):
    access = serializers.CharField(help_text="Access token for the `Authorization` header.")
    refresh = serializers.CharField(help_text="Refresh token for `POST /api/v1/auth/refresh/`.")


login_schema = extend_schema(
    tags=["Authentication"],
    summary="Log in",
    description=f"""
Authenticate with email and password. Returns an access and a refresh token.

Rate limited to {rate_limit("login")} per client.

{AUTH_NONE}
""",
    auth=[],                                   # public
    examples=[
        OpenApiExample(
            "Login",
            value={"email": "alice@example.com", "password": "Str0ng!Pass"},
            request_only=True,
        ),
    ],
    responses={
        200: OpenApiResponse(
            TokenPairSerializer,
            description="Logged in.",
            examples=[example("Token pair", {"access": "eyJ...", "refresh": "eyJ..."})],
        ),
        400: validation_error_response(
            "A required field is missing.",
            example("Missing fields", {"email": ["This field is required."]}),
        ),
        401: error_response(
            "Wrong email or password, or the account is inactive.",
            example("Invalid credentials", {"detail": "No active account found with the given credentials"}),
        ),
        429: throttled_response("login"),
    },
)


session_list_schema = extend_schema(
    tags=["Sessions"],
    summary="List sessions",
    description=f"""
List the current user's sessions, most recently used first.

{AUTH_REQUIRED}
""",
    responses={
        200: OpenApiResponse(
            SessionSerializer(many=True),
            description="The user's sessions.",
            # List endpoint: give ONE item; spectacular wraps it in a list.
            examples=[example("Sessions", {"id": "7c9e6679-...", "device_name": "Laptop"})],
        ),
        401: UNAUTHORIZED_RESPONSE,
    },
)


revoke_session_schema = extend_schema(
    tags=["Sessions"],
    summary="Revoke a session",
    description=f"""
Log out one device.

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
    request=None,                              # POST without a body
    responses={
        200: OpenApiResponse(MessageSerializer, description="Session revoked."),
        401: UNAUTHORIZED_RESPONSE,
        404: error_response("No active session with this ID.", example("Not found", {"detail": "Session not found."})),
    },
)
```

Guidelines:
- **Summary**: 2–5 words, sentence case, verb first ("Log in", "List
  sessions", "Update current user"). It is what Swagger shows in the list.
- **Description**: explain behaviour a client can't guess: side effects
  (revokes sessions, sends email), idempotency, what's read-only, limits,
  enumeration-safe responses, DEBUG-only fields. Use Markdown.
- **Request**: let spectacular derive it from the serializer when that's
  correct; override with `request=` otherwise (`None` for no body).
- **Responses**: every status code the code path can actually produce.
  Read the view, serializer `validate*()` methods, permission classes and
  exception handling to find them, then confirm with real responses.
- **Examples**: request examples use `request_only=True` on
  `extend_schema(examples=...)`; response examples go on
  `OpenApiResponse(examples=...)`. Take values from real responses (see §9).
  Keep realistic shapes, replace real tokens, IDs and secrets with obvious
  placeholders.
- A body that is one of two shapes (e.g. field errors or `{"detail": ...}`):
  `OpenApiResponse(response={"oneOf": [{"$ref": ".../ValidationError"}, {"$ref": ".../Error"}]})`.
  `Error` exists only if `ErrorSerializer` is used by some operation.

## 5. ViewSets, query parameters, pagination, 403, 204

```python
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter

item_viewset_schema = extend_schema_view(
    list=extend_schema(
        tags=["Items"],
        summary="List items",
        description=f"... Paginated, 20 per page. {AUTH_REQUIRED}",
        parameters=[
            # Only for params the view reads itself; filter backends and
            # pagination params are documented automatically.
            OpenApiParameter("status", str, OpenApiParameter.QUERY, enum=["open", "done"],
                             description="Only return items with this status."),
        ],
        responses={200: ItemSerializer(many=True), 401: UNAUTHORIZED_RESPONSE, 403: FORBIDDEN_RESPONSE},
    ),
    retrieve=extend_schema(tags=["Items"], summary="Get an item", description=..., responses={...404...}),
    create=extend_schema(tags=["Items"], summary="Create an item", ...),
    partial_update=extend_schema(tags=["Items"], summary="Update an item", ...),
    destroy=extend_schema(
        tags=["Items"],
        summary="Delete an item",
        description=...,
        responses={204: OpenApiResponse(description="Deleted. No body."), 401: ..., 404: ...},
    ),
)
```

- With `pagination_class` set (on the view or in
  `REST_FRAMEWORK["DEFAULT_PAGINATION_CLASS"]`), spectacular wraps list
  responses in the paginated envelope (`Paginated<Name>List`) and adds the
  paginator's query parameters (`page`, `limit`/`offset`, ...). Keep
  `responses={200: Serializer(many=True)}`; don't hand-build the envelope. A
  list example should still be a single item.
- For a filter whose values come from model choices, pass
  `enum=[value for value, _ in Model.Field.choices]` so the list stays in
  step with the model.
- Permissions that can deny an *authenticated* user (`IsAdminUser`,
  `DjangoModelPermissions`, object-level checks) mean a 403. `IsAuthenticated`
  alone doesn't produce 403 with token/JWT auth (it's 401).
- Router-generated routes that the ViewSet disables (`http_method_names`,
  ReadOnly viewsets) aren't in the schema; don't add `extend_schema` for
  actions that don't exist.
- `@action` methods take their own `@extend_schema(...)` directly.

## 6. Applying schemas to views

Views only carry the decorator, so the view code stays about behaviour:

```python
from .schema import login_schema, current_user_schema

@login_schema
class LoginView(TokenObtainPairView):
    ...

@current_user_schema                  # extend_schema_view(get=..., patch=...)
class CurrentUserView(generics.RetrieveUpdateAPIView):
    ...
```

Remove per-method `@extend_schema(...)` decorators and `inline_serializer`s
that the schema module replaces, together with imports that become unused.

Function views: the schema decorator goes **above** `@api_view`:

```python
@stats_schema
@api_view(["GET"])
def stats(request):
    ...
```

A library view used directly in `urls.py` (e.g. SimpleJWT's
`TokenRefreshView`) can be documented by a thin subclass in the app's
views, or with an `OpenApiViewExtension`, without touching `urls.py`
behaviour.

## 7. Serializer field descriptions

`help_text` shows up as the field description in Swagger. It never changes
behaviour, but put it on **serializer** fields, not model fields (model
`help_text` is part of migrations state).

```python
class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8,
                                     help_text="See the password rules in the endpoint description.")

    class Meta:
        model = User
        fields = ["email", "password", "first_name"]
        extra_kwargs = {
            "email": {"help_text": "Case-insensitive; stored lowercased."},
        }
```

For fields a library adds at runtime (SimpleJWT's `email`/`password` on
`TokenObtainPairSerializer`), set `self.fields[name].help_text` in
`__init__` after `super().__init__()`.

## 8. Regression tests

Put them wherever the project keeps cross-cutting tests (e.g. the health or
core app). Adapt the operation sets and scheme names. Uses Django's
`APITestCase`; for pytest-only projects, turn it into plain test functions.

When the API has more than one scheme (JWT for users plus an API key for
server-to-server calls, say), keep one set per non-default scheme, as
`APPLICATION_OPERATIONS` below, and check each operation against its set.

```python
from io import StringIO

import jsonschema
from django.core.management import call_command
from drf_spectacular.drainage import reset_generator_stats
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APITestCase


PUBLIC_OPERATIONS = {
    ("POST", "/api/v1/auth/login/"),
    ("GET", "/api/v1/health/"),
}

# Operations that use another scheme than the default one (optional).
APPLICATION_OPERATIONS = {
    ("GET", "/api/v1/apps/users/"),
}


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
        # OpenAPI 3.0 -> JSON Schema: inline $refs, map `nullable`.
        if isinstance(node, list):
            return [self.to_json_schema(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return self.to_json_schema(self.spec["components"]["schemas"][node["$ref"].split("/")[-1]])
        schema = {k: self.to_json_schema(v) for k, v in node.items() if k not in ("nullable", "readOnly", "writeOnly")}
        if node.get("nullable"):
            schema = {"anyOf": [schema, {"type": "null"}]}
        return schema

    def test_openapi_schema_is_valid_and_complete(self):
        reset_generator_stats()
        # Raises if any view can't be documented or the schema is invalid.
        call_command("spectacular", "--validate", "--fail-on-warn", stdout=StringIO(), stderr=StringIO())

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
                    self.assertEqual(operation.get("security"), [{"appKey": []}])
                    self.assertIn("401", operation["responses"])
                else:
                    self.assertEqual(operation.get("security"), [{"jwtAuth": []}])
                    self.assertIn("401", operation["responses"])

    def test_examples_match_their_schemas(self):
        for method, path, operation in self.operations():
            bodies = [("request", operation["requestBody"])] if "requestBody" in operation else []
            bodies += list(operation["responses"].items())
            for where, body in bodies:
                content = body.get("content", {}).get("application/json")
                if not content:
                    continue
                schema = self.to_json_schema(content["schema"])
                for name, ex in content.get("examples", {}).items():
                    with self.subTest(operation=f"{method} {path}", body=where, example=name):
                        jsonschema.validate(ex["value"], schema)
```

Public endpoints may still document 401 (bad credentials, invalid token in
the body); only assert "no `security`" for them.

## 9. Capturing real responses

Run requests through the test client against a **throwaway test database**
so examples match reality without touching dev data. Save as a scratch
script outside the repo and run it with the project's Python from the
project root:

```python
import json, os, sys

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")   # project's settings

import django
django.setup()

from django.db import connection
from django.test.utils import setup_test_environment
from rest_framework.test import APIClient

setup_test_environment()
old_name = connection.creation.create_test_db(verbosity=0, autoclobber=True)

try:
    client = APIClient()
    client.raise_request_exception = False        # see 500s as responses

    def show(label, response):
        try:
            body = response.json()
        except ValueError:
            body = response.content[:80]
        print(f"{label} -> {response.status_code}: {json.dumps(body)}")

    show("register ok", client.post("/api/v1/users/register/", {...}, format="json"))
    show("register duplicate", client.post("/api/v1/users/register/", {...}, format="json"))
    # log in, set client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}"),
    # hit every endpoint: success + each error branch you found in the code.
finally:
    connection.creation.destroy_test_db(old_name, verbosity=0)
```

Notes:
- `setup_test_environment()` doesn't force `DEBUG=False` the way the test
  runner does; DEBUG-only fields will appear. Note which fields are
  DEBUG-only in the docs.
- Clear the cache (`django.core.cache.cache.clear()`) before provoking
  throttles, and hit the throttled endpoint enough times to get the real 429
  body. If the cache is a database cache, it lives in the test database.
- With SQLite projects the test database is in memory; that's fine.

## 10. Custom authentication classes

drf-spectacular only knows DRF's and a few libraries' authentication
classes. For any other `BaseAuthentication` subclass (an API-key header, a
client package's JWT class), register an extension next to the class or in
the app's `schema.py` (imported at startup, e.g. from the views):

```python
from drf_spectacular.extensions import OpenApiAuthenticationExtension


class ApplicationKeyScheme(OpenApiAuthenticationExtension):
    target_class = "applications.authentication.ApplicationKeyAuthentication"
    name = "appKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "X-App-Key",
            "description": "The calling application's API key.",
        }
```

Then the operations using it show `security: [{"appKey": []}]`, the
scheme appears under **Authorize**, and the docs tests can check those
operations as their own set (§8).
