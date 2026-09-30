"""
The admin panel's integration guide. Facts that can change (URLs, token
lifetimes, rate limits, CORS, registration, email) are read from the running
configuration, and the API reference comes from the OpenAPI schema, so the
page is correct for whichever server shows it.
"""

import re
from functools import lru_cache

from django.conf import settings

from applications.models import Application
from config.openapi import describe_duration, rate_limit


REPOSITORY = "https://github.com/Nareshkumar124/jivo-auth.git"

CLIENT_INIT = settings.BASE_DIR / "packages" / "jivo-auth-client" / "jivo_auth" / "__init__.py"

PLACEHOLDER_APP = "your-app"


def client_version():
    try:
        match = re.search(r'__version__ = "([^"]+)"', CLIENT_INIT.read_text())
    except OSError:
        return None

    return match.group(1) if match else None


def email_status():
    mailer = settings.MAILERS["default"]

    if mailer["BACKEND"].endswith("console.EmailBackend"):
        return "console", "Printed to the server console (development)."

    host = mailer.get("OPTIONS", {}).get("host", "")

    if not host or host == "localhost":
        return "missing", "Not configured: reset and verification emails aren't sent."

    return "ok", f"Sent through {host}."


def rate_limits():
    return [
        ("Login", f"{rate_limit('login')} per account and IP, and {rate_limit('login_ip')} per IP"),
        ("Forgot password", rate_limit("forgot_password")),
        ("Reset password", rate_limit("reset_password")),
        ("Verify email", rate_limit("verify_email")),
        ("Resend verification", rate_limit("resend_verification")),
        ("Register", rate_limit("register")),
        ("Application user lookup", f"{rate_limit('application')} per application"),
        ("Everything else", f"{rate_limit('anon')} anonymous, {rate_limit('user')} per signed-in user"),
    ]


# ---- API reference, from the schema ---------------------------------------

AUTH_LABELS = {
    "jwtAuth": "Bearer",
    "appKey": "App key",
}


def _resolve(schema, components):
    while "$ref" in schema:
        schema = components[schema["$ref"].rsplit("/", 1)[-1]]

    return schema


def _fields(schema, components):
    schema = _resolve(schema, components)

    if schema.get("type") == "array":
        return _fields(schema.get("items", {}), components)

    required = set(schema.get("required", []))

    return [
        {"name": name, "required": name in required}
        for name in schema.get("properties", {})
    ]


@lru_cache(maxsize=1)
def api_reference():
    """Endpoints grouped by tag, from the same schema Swagger shows."""

    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator().get_schema(request=None, public=True)
    components = schema["components"]["schemas"]
    groups = {}

    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            body = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )

            success = next(
                (code for code in sorted(operation["responses"]) if code.startswith("2")),
                "",
            )
            response_schema = (
                operation["responses"].get(success, {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )

            query = [
                {"name": parameter["name"], "required": parameter.get("required", False), "query": True}
                for parameter in operation.get("parameters", [])
                if parameter["in"] == "query"
            ]

            security = [
                AUTH_LABELS.get(name, name)
                for requirement in operation.get("security", [])
                for name in requirement
            ]

            groups.setdefault(operation["tags"][0], []).append(
                {
                    "method": method.upper(),
                    "path": path,
                    "summary": operation.get("summary", ""),
                    "auth": " or ".join(security) or "None",
                    "request": query + (_fields(body, components) if body else []),
                    "status": success,
                    "response": _fields(response_schema, components) if response_schema else [],
                    "anchor": re.sub(r"[^a-z0-9]+", "-", f"{method}-{path}".lower()).strip("-"),
                }
            )

    order = [tag["name"] for tag in settings.SPECTACULAR_SETTINGS["TAGS"]]

    return sorted(
        ({"tag": tag, "endpoints": endpoints} for tag, endpoints in groups.items()),
        key=lambda group: order.index(group["tag"]) if group["tag"] in order else len(order),
    )


# ---- Code samples ---------------------------------------------------------
# Plain strings with __BASE__ / __ISSUER__ / __APP__ placeholders, so braces
# in Python and JavaScript need no escaping; the template escapes the HTML.

INSTALL_UV = '''uv add "jivo-auth-client @ git+__REPOSITORY__#subdirectory=packages/jivo-auth-client" --rev <commit>'''

INSTALL_PIP = '''# requirements.txt
jivo-auth-client @ git+__REPOSITORY__@<commit>#subdirectory=packages/jivo-auth-client'''

SETTINGS = '''# settings.py
INSTALLED_APPS = [
    # ...
    "rest_framework",
    "jivo_auth",            # enables the startup checks
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "jivo_auth.authentication.JivoJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

JIVO_AUTH = {
    "URL": "__BASE__",
    "APP": "__APP__",
}'''

API_CORS = '''# pip install django-cors-headers
INSTALLED_APPS += ["corsheaders"]
MIDDLEWARE.insert(0, "corsheaders.middleware.CorsMiddleware")
CORS_ALLOWED_ORIGINS = ["https://your-frontend.jivo.in"]
# Tokens travel in the Authorization header (allowed by default), not in
# cookies, so leave CORS_ALLOW_CREDENTIALS off.'''

VIEWS = '''from rest_framework import permissions, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response


class OrderViewSet(viewsets.ModelViewSet):      # protected by default
    serializer_class = OrderSerializer
    ...


@api_view(["GET"])
@permission_classes([permissions.AllowAny])    # public
def health(request):
    return Response({"status": "ok"})'''

WHO_AM_I = '''@api_view(["GET"])
def me(request):
    user = request.user          # jivo_auth.users.JivoUser
    return Response({
        "id": str(user.id),      # uuid.UUID, the same in every Jivo app
        "email": user.email,     # verified, lowercase
        "apps": user.apps,       # ("__APP__", ...)
    })'''

STATELESS = '''class Order(models.Model):
    created_by = models.UUIDField(db_index=True)     # Jivo user ID


class OrderViewSet(viewsets.ModelViewSet):
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user.id)

    def get_queryset(self):
        return Order.objects.filter(created_by=self.request.user.id)'''

LOCAL_USERS = '''JIVO_AUTH = {
    "URL": "__BASE__",
    "APP": "__APP__",
    "LOCAL_USERS": True,
    "LOCAL_USER_ID_FIELD": "username",   # where the Jivo user ID is stored
}


class Order(models.Model):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)


class OrderSerializer(serializers.ModelSerializer):
    created_by = serializers.HiddenField(default=serializers.CurrentUserDefault())'''

PERMISSIONS = '''class IsCreator(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        return obj.created_by == request.user.id    # stateless
        # return obj.created_by == request.user     # local users


class Role(models.Model):
    jivo_user_id = models.UUIDField()
    name = models.CharField(max_length=30)       # "manager", "clerk", ...


class IsManager(permissions.BasePermission):
    message = "Only managers can do this."

    def has_permission(self, request, view):
        return Role.objects.filter(
            jivo_user_id=request.user.id, name="manager"
        ).exists()'''

FRONTEND = '''const AUTH = "__BASE__/api/v1";
const API = "https://your-api.jivo.in";
const STORAGE_KEY = "jivoTokens";

// Always read the stored copy: another tab may have refreshed meanwhile.
const loadTokens = () => JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");

function saveTokens(tokens) {
  if (tokens) localStorage.setItem(STORAGE_KEY, JSON.stringify(tokens));
  else localStorage.removeItem(STORAGE_KEY);
}

function postJSON(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function login(email, password) {
  const response = await postJSON(`${AUTH}/auth/login/`, {
    email, password, device_name: "__APP__ web",
  });
  const data = await response.json();

  if (response.ok) return saveTokens(data);
  if (data.code === "email_not_verified") throw new Error("verify-email");
  if (response.status === 429) throw new Error("too-many-attempts");
  throw new Error("wrong-credentials");
}

let refreshing = null;

function refresh(rejectedAccess) {
  const tokens = loadTokens();

  if (!tokens) return Promise.reject(new Error("logged-out"));

  // Someone else already refreshed: just retry with the newer token.
  if (tokens.access !== rejectedAccess) return Promise.resolve();

  // One refresh at a time; concurrent callers wait for the same one.
  refreshing ??= postJSON(`${AUTH}/auth/refresh/`, { refresh: tokens.refresh })
    .then(async (response) => {
      if (!response.ok) {
        saveTokens(null);            // session ended: log in again
        throw new Error("logged-out");
      }
      saveTokens(await response.json());
    })
    .finally(() => { refreshing = null; });

  return refreshing;
}

export async function api(path, options = {}) {
  const send = (access) => fetch(API + path, {
    ...options,
    headers: { ...options.headers, Authorization: `Bearer ${access}` },
  });

  const tokens = loadTokens();
  if (!tokens) throw new Error("logged-out");

  let response = await send(tokens.access);

  if (response.status === 401) {
    await refresh(tokens.access);    // throws "logged-out" if it can't
    response = await send(loadTokens().access);
  }

  return response;                   // 403: no access to "__APP__", or your own rules
}

export async function logout() {
  const tokens = loadTokens();
  if (tokens) await postJSON(`${AUTH}/auth/logout/`, { refresh: tokens.refresh });
  saveTokens(null);
}'''

LOOKUP = '''JIVO_AUTH = {
    # ...
    "API_KEY": os.environ["JIVO_AUTH_API_KEY"],   # server-side secret
}

from jivo_auth.client import AuthClient
from jivo_auth.exceptions import AuthServiceError

client = AuthClient()

try:
    everyone = client.get_users()              # all users with access to __APP__
    people = client.get_users(jivo_user_ids)   # only these IDs; batched 100 per call
except AuthServiceError as exc:                # .status, .data; AuthServiceUnavailable if unreachable
    ...

# [{"id": "3fa8...", "email": "alice@jivo.in", "first_name": "Alice",
#   "last_name": "Smith", "employee_code": "JIVO1234", "is_active": True}, ...]'''

TEST_FORCE = '''import uuid
from rest_framework.test import APITestCase
from jivo_auth.users import JivoUser


class OrderTests(APITestCase):
    def setUp(self):
        self.user = JivoUser(id=uuid.uuid4(), email="tester@jivo.in", apps=("__APP__",))
        self.client.force_authenticate(self.user)

    def test_list(self):
        self.assertEqual(self.client.get("/orders/").status_code, 200)'''

TEST_TOKENS = '''# tests/jivo.py
import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import override_settings

_TEST_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

TEST_PUBLIC_KEY = _TEST_KEY.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()


def make_access_token(user_id=None, email="tester@jivo.in", apps=("__APP__",), **claims):
    now = int(time.time())
    payload = {
        "token_type": "access",
        "sub": str(user_id or uuid.uuid4()),
        "email": email,
        "apps": list(apps),
        "iss": "__ISSUER__",
        "iat": now,
        "exp": now + 900,
        "jti": uuid.uuid4().hex,
        **claims,
    }
    return jwt.encode(payload, _TEST_KEY, algorithm="RS256")


# Verify with the test key instead of fetching Jivo Auth's.
use_test_keys = override_settings(
    JIVO_AUTH={"URL": "__BASE__", "APP": "__APP__", "PUBLIC_KEY": TEST_PUBLIC_KEY},
)'''

SMOKE_TEST = '''# 1. Your server reaches Jivo Auth and its signing keys.
curl -s __BASE__/api/v1/health/
curl -s __BASE__/.well-known/jwks.json

# 2. Your configuration passes the startup checks.
python manage.py check

# 3. Sign in as a test account granted "__APP__", and keep the access token.
ACCESS=$(curl -s -X POST __BASE__/api/v1/auth/login/ \\
  -H "Content-Type: application/json" \\
  -d '{"email": "tester@jivo.in", "password": "<password>", "device_name": "smoke test"}' \\
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access"])')

# 4. The token verifies inside your app, with "__APP__" in its apps claim.
python manage.py shell -c "from jivo_auth.tokens import decode_access_token as d; print(d('$ACCESS'))"

# 5. Your API accepts it: expect your view's answer, not 401 or 403.
curl -i https://your-api.jivo.in/orders/ -H "Authorization: Bearer $ACCESS"'''

SAMPLES = {
    "install_uv": INSTALL_UV,
    "install_pip": INSTALL_PIP,
    "settings": SETTINGS,
    "api_cors": API_CORS,
    "views": VIEWS,
    "who_am_i": WHO_AM_I,
    "stateless": STATELESS,
    "local_users": LOCAL_USERS,
    "permissions": PERMISSIONS,
    "frontend": FRONTEND,
    "lookup": LOOKUP,
    "test_force": TEST_FORCE,
    "test_tokens": TEST_TOKENS,
    "smoke_test": SMOKE_TEST,
}


def fill(sample, **values):
    for name, value in values.items():
        sample = sample.replace(f"__{name.upper()}__", value)

    return sample


def build_docs(request):
    can_see_apps = request.user.has_perm("applications.view_application")
    applications = (
        list(Application.objects.filter(is_active=True).order_by("slug"))
        if can_see_apps
        else []
    )

    slugs = [app.slug for app in applications]
    requested = request.GET.get("app", "")
    slug = requested if requested in slugs else (slugs[0] if slugs else PLACEHOLDER_APP)

    base = settings.PUBLIC_URL
    issuer = settings.SIMPLE_JWT["ISSUER"]
    jwk = settings.JWT_PUBLIC_JWK
    email_state, email_text = email_status()

    values = {"base": base, "issuer": issuer, "app": slug, "repository": REPOSITORY}

    return {
        "slug": slug,
        "applications": applications,
        "can_see_apps": can_see_apps,
        "facts": {
            "base": base,
            "issuer": issuer,
            "jwks_url": f"{base}/.well-known/jwks.json",
            "docs_url": f"{base}/api/docs/",
            "algorithm": settings.JWT_ALGORITHM,
            "kid": jwk["kid"] if jwk else None,
            "access": describe_duration(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"]),
            "refresh": describe_duration(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]),
            "grace": int(settings.REFRESH_TOKEN_REUSE_GRACE.total_seconds()),
            "client_version": client_version(),
            "repository": REPOSITORY,
        },
        "status": {
            "cors_origins": list(settings.CORS_ALLOWED_ORIGINS),
            "cors_patterns": list(settings.CORS_ALLOWED_ORIGIN_REGEXES),
            "debug": settings.DEBUG,
            "registration": settings.REGISTRATION_ENABLED,
            "verified_email": settings.REQUIRE_VERIFIED_EMAIL,
            "email_state": email_state,
            "email_text": email_text,
        },
        "rate_limits": rate_limits(),
        "api_groups": api_reference(),
        "samples": {name: fill(sample, **values) for name, sample in SAMPLES.items()},
    }
