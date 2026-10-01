# Integrating a Django REST Framework API

This guide protects a **Django REST Framework** API with Jivo login.
Your frontend (React, Vue, a mobile app) logs users in at
**https://auth.jivo.in**, then calls your API with the access token. Your
API verifies each token itself, locally, using the `jivo-auth-client`
package (import name `jivo_auth`).

The examples use an application with the slug **`oms`**. Its frontend is
at `https://oms.jivo.in` and its API at `https://api.oms.jivo.in`.

> Building a server-rendered site with Django templates or the Django admin
> instead? See [session login](integrate-django.md#b-server-rendered-django-site-templates-admin).

**Contents**

1. [The live service](#the-live-service)
2. [How it works](#how-it-works)
3. [Before you start](#before-you-start)
4. [Install](#install)
5. [Configure](#configure)
6. [Protect your views](#protect-your-views)
7. [Who is the user?](#who-is-the-user)
8. [Roles and permissions](#roles-and-permissions)
9. [Frontend: logging in and calling your API](#frontend-logging-in-and-calling-your-api)
10. [API reference](#api-reference)
11. [Looking up other users](#looking-up-other-users)
12. [Testing](#testing)
13. [Check your integration against production](#check-your-integration-against-production)
14. [Going live checklist](#going-live-checklist)
15. [Troubleshooting](#troubleshooting)
16. [Settings reference](#settings-reference)

---

## The live service

These values were checked against production on **2026-09-30**. The live
API schema matched the code in the repository at commit `cda275f`.

| | |
|---|---|
| Base URL | `https://auth.jivo.in` (HTTP redirects to HTTPS) |
| API reference | [https://auth.jivo.in/api/docs/](https://auth.jivo.in/api/docs/) (Swagger), schema at `/api/schema/` |
| Health | `GET https://auth.jivo.in/api/v1/health/` → `{"status": "ok", "service": "auth-service"}` |
| Signing keys | `https://auth.jivo.in/.well-known/jwks.json`: one RS256 key (2048-bit RSA), cached for 5 minutes (`Cache-Control: public, max-age=300`) |
| Token issuer (`iss`) | `https://auth.jivo.in` |
| Access token | 15 minutes |
| Refresh token | 30 days, new one on every refresh; the previous one is accepted again for 30 seconds |
| Client package | `jivo-auth-client` 0.2.0, at commit `cda275f` |

> **Current status: check these first**
>
> - **Browser frontends are blocked by CORS.** No origin is allowed yet: a
>   preflight from, say, `https://oms.jivo.in` gets no
>   `Access-Control-Allow-Origin` header. Ask the Jivo Auth administrator to
>   add your frontend's origin to `CORS_ALLOWED_ORIGINS` before you test
>   login from a browser. Mobile apps and server-to-server calls aren't
>   affected.
> - **Self-registration is off.** `POST /api/v1/users/register/` answers
>   `403 {"detail": "Registration is disabled."}`. Accounts are created by
>   an administrator, who also grants them your application.
> - **Emails aren't sent yet** (SMTP isn't configured). Forgot-password
>   and verification links won't arrive. Until then, an administrator
>   resets passwords in the admin panel. Accounts created by an
>   administrator are verified already.

## How it works

```mermaid
sequenceDiagram
    autonumber
    participant F as Frontend (oms.jivo.in)
    participant A as Jivo Auth (auth.jivo.in)
    participant Y as Your DRF API

    F->>A: POST /api/v1/auth/login/ {email, password}
    A-->>F: {access, refresh}
    F->>Y: GET /orders/  Authorization: Bearer <access>
    Note over Y: Verifies the token locally:<br/>signature, expiry, issuer,<br/>"oms" in apps
    Y-->>F: 200 [orders]
    Note over F: 15 minutes later the access token expires
    F->>Y: GET /orders/  (expired token)
    Y-->>F: 401 Token has expired.
    F->>A: POST /api/v1/auth/refresh/ {refresh}
    A-->>F: {access, refresh}  (both new)
    F->>Y: GET /orders/  (new token)
    Y-->>F: 200 [orders]
```

The key points:

- **Your API never sees passwords** and never calls Jivo Auth per request.
  The package only fetches Jivo Auth's public key, once, from
  `https://auth.jivo.in/.well-known/jwks.json`, and caches it. Your API
  keeps working through short Jivo Auth outages.
- **Access tokens last 15 minutes. Refresh tokens last 30 days** and
  change on every refresh.
- **Access is per application.** A user needs `oms` in their token's
  `apps` claim; everyone else gets 403 from your API.

## Before you start

| You need | Where from |
|---|---|
| Python 3.10+, Django 4.2+, DRF | Your project |
| Your application's **slug** (`oms`) | A Jivo Auth administrator ([registering an application](register-an-application.md)) |
| Your frontend's origin **allowed for CORS** at Jivo Auth | The same administrator. Currently no origin is allowed ([status](#the-live-service)). |
| A **test account** granted access to `oms` | The same administrator (self-registration is off) |
| An **API key** | Only if your server looks up users ([below](#looking-up-other-users)) |

## Install

The package is in the public `Nareshkumar124/jivo-auth` repository, so
installing it needs no credentials.

```bash
# uv
uv add "jivo-auth-client @ git+https://github.com/Nareshkumar124/jivo-auth.git#subdirectory=packages/jivo-auth-client" --rev cda275f072c8f3595bce30382875fbe2d42ade6f
```

```text
# requirements.txt
jivo-auth-client @ git+https://github.com/Nareshkumar124/jivo-auth.git@cda275f072c8f3595bce30382875fbe2d42ade6f#subdirectory=packages/jivo-auth-client
```

That commit carries client **0.2.0**, the version this guide describes.
Pin a commit or tag rather than `main`. Docker builds can install it
straight from Git too, or from a wheel built with
`uv build packages/jivo-auth-client`.

The package installs DRF and PyJWT (with `cryptography`) if you don't
have them.

## Configure

```python
# settings.py
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
    "URL": "https://auth.jivo.in",
    "APP": "oms",
}
```

Keys can also come from environment variables, e.g. `JIVO_AUTH_URL` and
`JIVO_AUTH_APP`, when they're missing from the dict.

Run `python manage.py check`. It reports:

- a missing `APP`: without it **every user is refused**, so a forgotten
  setting can't let every Jivo account in
- misspelled keys
- no way to verify tokens

If your API really is for every Jivo user, set `"ALLOW_ALL_USERS": True`
instead of `APP`.

**If your frontend and API are on different origins** (`oms.jivo.in` →
`api.oms.jivo.in`), your API needs CORS too:

```python
# pip install django-cors-headers
INSTALLED_APPS += ["corsheaders"]
MIDDLEWARE.insert(0, "corsheaders.middleware.CorsMiddleware")
CORS_ALLOWED_ORIGINS = ["https://oms.jivo.in"]
# The Authorization header is allowed by default. Tokens aren't cookies,
# so leave CORS_ALLOW_CREDENTIALS off.
```

## Protect your views

With the settings above, **every view requires a valid Jivo token**. Opt
out explicitly where you need to:

```python
from rest_framework import permissions, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

class OrderViewSet(viewsets.ModelViewSet):      # protected by default
    serializer_class = OrderSerializer
    ...

@api_view(["GET"])
@permission_classes([permissions.AllowAny])    # public
def health(request):
    return Response({"status": "ok"})
```

A request is rejected before your view runs:

| Response | `detail` | What the client should do |
|---|---|---|
| **401** | `Authentication credentials were not provided.` | Log in. |
| **401** | `Token has expired.` | Refresh, then retry once. |
| **401** | `Invalid token.` | Refresh; if that fails, log in again. |
| **401** | `Access token required.` | Bug: it sent the refresh token. Send the access token. |
| **403** | `Your account does not have access to this application.` | Show "no access". Refreshing won't help until an admin grants access. |

`JivoJWTAuthentication` only handles `Authorization: Bearer ...`. Other
schemes are left to other authentication classes, so you can keep, say,
`SessionAuthentication` for the browsable API:

```python
"DEFAULT_AUTHENTICATION_CLASSES": [
    "jivo_auth.authentication.JivoJWTAuthentication",
    "rest_framework.authentication.SessionAuthentication",
],
```

**Swagger (drf-spectacular):** the package registers a `jivoAuth` bearer
security scheme automatically. Your Swagger UI's **Authorize** button then
takes a Jivo access token.

## Who is the user?

By default `request.user` is a lightweight **`JivoUser`** built from the
token, with no database involved:

```python
@api_view(["GET"])
def me(request):
    user = request.user          # jivo_auth.users.JivoUser
    return Response({
        "id": str(user.id),      # uuid.UUID, the same across every Jivo app
        "email": user.email,     # verified, lowercase
        "apps": user.apps,       # ("oms", ...)
    })
```

| Attribute | Value |
|---|---|
| `id`, `pk` | The Jivo user ID (`uuid.UUID`) |
| `email` | The user's email |
| `apps` | Tuple of application slugs; `has_app("oms")` checks one |
| `claims` | The full verified token payload |
| `is_authenticated` | `True` |
| `is_staff`, `is_superuser` | Always `False` |
| `has_perm(...)` | Always `False` |

`request.auth` is the raw access token string, if you need to forward it.
DRF's `UserRateThrottle` works as usual, keyed on the user's ID.

### Pick a user mode

| | Stateless (default) | Local users |
|---|---|---|
| `request.user` | `JivoUser` | A row of your `AUTH_USER_MODEL`, created on first request |
| Store "who did this" | `models.UUIDField()` holding the Jivo ID | A normal `ForeignKey` |
| Per-user permissions, groups, `is_staff` | Your own tables keyed by Jivo ID | Django's built-ins |
| Database hit per request | None | One lookup |
| Turn on with | Nothing | `"LOCAL_USERS": True` |

**Stateless: record the Jivo ID.**

```python
class Order(models.Model):
    created_by = models.UUIDField(db_index=True)     # Jivo user ID
    ...

class OrderViewSet(viewsets.ModelViewSet):
    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user.id)

    def get_queryset(self):
        return Order.objects.filter(created_by=self.request.user.id)
```

**Local users: real foreign keys.**

```python
JIVO_AUTH = {
    "URL": "https://auth.jivo.in",
    "APP": "oms",
    "LOCAL_USERS": True,
    "LOCAL_USER_ID_FIELD": "username",   # where the Jivo ID is stored
}
```

```python
class Order(models.Model):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

class OrderSerializer(serializers.ModelSerializer):
    created_by = serializers.HiddenField(default=serializers.CurrentUserDefault())
    ...
```

How local users behave:

- **Creation:** a row is created the first time a user calls your API,
  with `username` set to the Jivo ID, `email` from the token, and an
  unusable password. The email follows later tokens.
- **The token's claims** are available as `request.user.jivo_claims`.
- **Blocking locally:** setting `is_active=False` blocks that user in your
  API only, with a 401 `User account is disabled.`.
- **Custom user model:** point `LOCAL_USER_ID_FIELD` at any unique field,
  e.g. `auth_id = models.UUIDField(null=True, unique=True)`, or at `"id"`
  if your primary key is a UUID that should equal the Jivo ID. New rows
  get the email as their `username` (the Jivo ID if that's taken).
- **An application that had its own users** keeps its user table and
  foreign keys: import the users into Jivo Auth, store each one's Jivo ID
  in `auth_id`, and switch. See
  [Moving an existing application onto Jivo Auth](integrate-django.md#moving-an-existing-application-onto-jivo-auth).

## Roles and permissions

Jivo Auth decides **who the user is** and **whether they may use `oms`**.
Everything finer, such as managers vs. clerks or record ownership, belongs
to your application.

**Object ownership:**

```python
class IsCreator(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        return obj.created_by == request.user.id    # stateless
        # return obj.created_by == request.user     # local users
```

**Roles, stateless:** keep a small table keyed by Jivo ID:

```python
class Role(models.Model):
    jivo_user_id = models.UUIDField()
    name = models.CharField(max_length=30)       # "manager", "clerk", ...

class IsManager(permissions.BasePermission):
    message = "Only managers can do this."

    def has_permission(self, request, view):
        return Role.objects.filter(
            jivo_user_id=request.user.id, name="manager"
        ).exists()
```

**Roles, local users:** use Django groups and permissions:
`request.user.has_perm("orders.approve_order")`, or DRF's
`DjangoModelPermissions` and `IsAdminUser`.

## Frontend: logging in and calling your API

The frontend needs three Jivo Auth calls: `POST /api/v1/auth/login/`,
`POST /api/v1/auth/refresh/` and `POST /api/v1/auth/logout/`. The
[API reference](#api-reference) below lists every endpoint with its
request, response and errors.

Forgotten passwords: link to `https://auth.jivo.in/forgot-password/`. Jivo
Auth hosts the whole reset flow, but it depends on email, which isn't
configured yet ([status](#the-live-service)).

A minimal client, refreshing once and on demand, however many requests fail
at the same time:

```js
const AUTH = "https://auth.jivo.in/api/v1";
const API = "https://api.oms.jivo.in";
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
    email, password, device_name: "OMS web",
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

  if (response.status === 403) {
    // Either your own permission classes, or no access to "oms" at all:
    // check the `detail` before showing "no access".
  }

  return response;
}

export async function logout() {
  const tokens = loadTokens();
  if (tokens) await postJSON(`${AUTH}/auth/logout/`, { refresh: tokens.refresh });
  saveTokens(null);
}
```

Things to get right:

- **Save both tokens after every refresh.** A just-used refresh token keeps
  working for 30 more seconds, which covers requests that were already in
  flight. After that, sending it again is treated as theft and **ends the
  session**.
- **Where to keep tokens.** `localStorage` keeps the user logged in across
  reloads, but any script injected into your page can read it. Keep a
  strict Content-Security-Policy. On mobile, use the Keychain (iOS) or
  Keystore (Android).
- **Showing "no access" early.** Decode the access token's payload
  (base64url of the middle part) and check `apps` includes `"oms"` right
  after login.
- **When changes arrive.** If an admin removes a user's access, or the user
  changes their password elsewhere, your API keeps accepting their current
  access token for up to 15 minutes. The next refresh then fails, or
  returns a token without `oms`.

## API reference

All paths are relative to `https://auth.jivo.in`. Request and response
bodies are JSON (`Content-Type: application/json`). "Bearer" means
`Authorization: Bearer <access token>`, and "App key" means
`X-Jivo-App-Key: <API key>`. [Swagger](https://auth.jivo.in/api/docs/)
has the same reference with examples, and lets you try calls.

### Tokens

| Endpoint | Auth | Request body | Success |
|---|---|---|---|
| `POST /api/v1/auth/login/` | none | `email`, `password`, `device_name`? | `200 {access, refresh}` |
| `POST /api/v1/auth/refresh/` | none | `refresh` | `200 {access, refresh}` (both new) |
| `POST /api/v1/auth/logout/` | none | `refresh` | `200 {message}` |
| `POST /api/v1/auth/verify/` | none | `token` (access or refresh) | `200 {}` |
| `GET /.well-known/jwks.json` | none | | `200 {keys: [{kty, use, alg, kid, n, e}]}` |

- **Login.** `email` is case-insensitive. `device_name` labels the session
  in the user's session list (e.g. `"OMS web"`).
- **Refresh.** Always store both returned tokens. For 30 seconds after a
  refresh, the refresh token that was sent still works and returns the
  same new pair, which covers requests that were already in flight.
  Sending it after that is treated as token theft: the whole session is
  revoked.
- **Logout** needs no access token (an `Authorization` header is ignored),
  so it works after the access token has expired.
- **Verify** is for clients that can't check a JWT themselves. Your DRF API
  doesn't need it.

### Account and password

| Endpoint | Auth | Request body | Success |
|---|---|---|---|
| `GET /api/v1/users/me/` | Bearer | | `200` profile (below) |
| `PATCH /api/v1/users/me/` | Bearer | `first_name`?, `last_name`? | `200` profile |
| `POST /api/v1/users/change-password/` | Bearer | `current_password`, `new_password`, `new_password_confirm` | `200 {message}`; every session of the user ends |
| `POST /api/v1/users/forgot-password/` | none | `email` | `200 {message}`, the same whether or not the account exists |
| `POST /api/v1/users/reset-password/` | none | `token`, `new_password`, `new_password_confirm` | `200 {message}`; every session ends |
| `POST /api/v1/users/verify-email/` | none | `token` | `200 {message}` |
| `POST /api/v1/users/resend-verification/` | none | `email` | `200 {message}`, the same whether or not the account exists |
| `POST /api/v1/users/register/` | none | `email`, `password`, `password_confirm`, `first_name`?, `last_name`? | **Disabled** on production: `403` |

The profile returned by `/users/me/`:

```json
{
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "email": "alice@jivo.in",
  "first_name": "Alice",
  "last_name": "Smith",
  "employee_code": "JIVO1234",
  "is_active": true,
  "is_verified": true,
  "apps": ["oms"],
  "created_at": "2026-09-30T06:07:16.357563Z",
  "updated_at": "2026-09-30T06:07:16.399186Z"
}
```

`employee_code` is the person's code in Jivo's HR records, uppercase, or
`""` if an administrator hasn't set one. It's unique when set, and
read-only here: `PATCH` changes only the names.

Passwords need at least 8 characters with an uppercase letter, a lowercase
letter, a digit and a special character. Common passwords and ones too
similar to the email or name are refused.

### Sessions (one per signed-in device)

| Endpoint | Auth | Success |
|---|---|---|
| `GET /api/v1/auth/sessions/` | Bearer | `200 [{id, device_name, ip_address, user_agent, created_at, last_used_at, revoked_at, is_active}]` |
| `POST /api/v1/auth/sessions/{id}/revoke/` | Bearer | `200 {message}`; `404` if it isn't an active session of this user |
| `POST /api/v1/auth/sessions/revoke-all/` | Bearer | `200 {message, revoked_sessions}` |

### Server-to-server

| Endpoint | Auth | Success |
|---|---|---|
| `GET /api/v1/apps/users/?id=<uuid>&id=<uuid>` | App key | `200 [{id, email, first_name, last_name, employee_code, is_active}]`: only users with access to your application; at most 100 `id`s |

Use `AuthClient().get_users()` rather than calling this yourself
([below](#looking-up-other-users)).

### Errors

Errors other than validation errors have a `detail`, and token errors add
a machine-readable `code`:

```json
{"detail": "Token is invalid", "code": "token_not_valid"}
```

Validation errors (400) map each field to its messages; errors not tied to
a field go under `non_field_errors`:

```json
{"email": ["This field is required."], "password": ["This field is required."]}
```

What the frontend should do with each error from Jivo Auth:

| Status | Body | Meaning, and what to do |
|---|---|---|
| 401 | `No active account found with the given credentials` | Login: wrong email or password, or the account is deactivated. |
| 401 | `code: email_not_verified` | Login: the email isn't verified yet. |
| 401 | `code: token_not_valid` | Refresh: the token expired, was already used, or its session was logged out or revoked. Send the user to sign in. |
| 401 | `code: no_active_account` | Refresh: the account was deactivated. Sign the user out. |
| 403 | `Registration is disabled.` | Register: turned off on production. |
| 429 | `Request was throttled. Expected available in N seconds.` | Too many attempts. Wait for the number of seconds in the `Retry-After` header. |

Your own API's errors (from `JivoJWTAuthentication`) are listed under
[Protect your views](#protect-your-views).

### Rate limits

Per client IP unless stated. Over the limit, the answer is `429` with a
`Retry-After` header.

| Endpoint | Limit |
|---|---|
| Login | 5 per minute per account and IP, and 30 per minute per IP |
| Forgot password | 3 per minute |
| Reset password | 5 per minute |
| Verify email | 10 per minute |
| Resend verification | 3 per minute |
| Register | 5 per hour |
| `/api/v1/apps/users/` | 10,000 per hour per application |
| Everything else | 100 per minute anonymous, 1,000 per hour per signed-in user |

## Looking up other users

To show who created an order, or fill an "assign to" picker, your server
can ask Jivo Auth about the users who have access to `oms`. This needs your
**API key**. It's a server-side secret; never send it to the frontend.

```python
JIVO_AUTH = {
    # ...
    "API_KEY": os.environ["JIVO_AUTH_API_KEY"],
}
```

```python
from jivo_auth.client import AuthClient
from jivo_auth.exceptions import AuthServiceError

client = AuthClient()

try:
    everyone = client.get_users()                # all users with access to oms
    people = client.get_users(order_creator_ids) # only these Jivo IDs; batched 100 per call
except AuthServiceError as exc:                  # .status, .data; AuthServiceUnavailable if unreachable
    ...

# [{"id": "3fa8...", "email": "alice@jivo.in", "first_name": "Alice",
#   "last_name": "Smith", "employee_code": "JIVO1234", "is_active": True}, ...]
```

Users without access to `oms` are never returned. Cache the results if you
call this often. `employee_code` is the way to match Jivo users to your own
HR or ERP records (it's `""` when not set). It isn't a token claim, so read
it from here or from `/api/v1/users/me/`.

**Calling another Jivo API on the user's behalf:** forward their token,
`headers={"Authorization": f"Bearer {request.auth}"}`. The other API
accepts it if the user has access to that application too.

## Testing

Don't call auth.jivo.in from tests. Pick the level you need.

**Most view tests: skip authentication.**

```python
import uuid
from rest_framework.test import APITestCase
from jivo_auth.users import JivoUser

class OrderTests(APITestCase):
    def setUp(self):
        self.user = JivoUser(id=uuid.uuid4(), email="tester@jivo.in", apps=("oms",))
        self.client.force_authenticate(self.user)

    def test_list(self):
        response = self.client.get("/orders/")
        self.assertEqual(response.status_code, 200)
```

With `LOCAL_USERS`, create a `User` row and pass it to
`force_authenticate`.

**Tests that exercise the real token check.** Sign your own tokens with a
test key:

```python
# tests/jivo.py
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


def make_access_token(user_id=None, email="tester@jivo.in", apps=("oms",), **claims):
    now = int(time.time())
    payload = {
        "token_type": "access",
        "sub": str(user_id or uuid.uuid4()),
        "email": email,
        "apps": list(apps),
        "iss": "https://auth.jivo.in",
        "iat": now,
        "exp": now + 900,
        "jti": uuid.uuid4().hex,
        **claims,
    }
    return jwt.encode(payload, _TEST_KEY, algorithm="RS256")


# Verify with the test key instead of fetching Jivo Auth's.
use_test_keys = override_settings(
    JIVO_AUTH={
        "URL": "https://auth.jivo.in",
        "APP": "oms",
        "PUBLIC_KEY": TEST_PUBLIC_KEY,
    },
)
```

```python
from rest_framework.test import APITestCase
from .jivo import make_access_token, use_test_keys

@use_test_keys
class AuthTests(APITestCase):
    def test_other_application_is_refused(self):
        token = make_access_token(apps=("ecom",))
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        self.assertEqual(self.client.get("/orders/").status_code, 403)

    def test_expired_token_is_refused(self):
        token = make_access_token(exp=0)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        self.assertEqual(self.client.get("/orders/").status_code, 401)
```

## Check your integration against production

Run these from the server that hosts your API, with a test account an
administrator created and granted `oms`. Keep repeated tries in mind: login
allows 5 attempts per minute per account, and every attempt shows in Jivo
Auth's audit log.

```bash
# 1. Your server reaches Jivo Auth and its signing keys.
curl -s https://auth.jivo.in/api/v1/health/
curl -s https://auth.jivo.in/.well-known/jwks.json

# 2. Your configuration passes the startup checks.
python manage.py check

# 3. Sign in as the test account and keep the access token.
ACCESS=$(curl -s -X POST https://auth.jivo.in/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{"email": "tester@jivo.in", "password": "<password>", "device_name": "smoke test"}' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access"])')

# 4. The token verifies inside your app, with "oms" in its apps claim.
python manage.py shell -c "from jivo_auth.tokens import decode_access_token as d; print(d('$ACCESS'))"

# 5. Your API accepts it: expect 200 (or your view's own answer), not 401/403.
curl -i https://api.oms.jivo.in/orders/ -H "Authorization: Bearer $ACCESS"
```

Step 4 prints the claims (`sub`, `email`, `apps`, `iss`, `exp`, ...). Most
failures show up here:

- **`Invalid token.`:** check `URL` and your server's outbound HTTPS.
- **`Token has expired.`:** check the server's clock.
- **`apps` doesn't list `oms`:** the account wasn't granted access.

## Going live checklist

- [ ] `JIVO_AUTH["URL"]` is exactly `https://auth.jivo.in`: https, no
      trailing path.
- [ ] `JIVO_AUTH["APP"]` is your slug, and `python manage.py check` is
      clean.
- [ ] Your server can make outbound HTTPS requests to `auth.jivo.in`, to
      fetch the public key on the first request and after a key change.
- [ ] Your server's clock is synced (NTP). Up to 10 seconds of drift is
      tolerated.
- [ ] Jivo Auth allows your frontend's origin (CORS; none is allowed yet),
      and your API allows it too if it's on a different origin.
- [ ] The frontend stores **both** tokens after every refresh, and handles
      401 (refresh, then log in) and 403 (no access).
- [ ] Real users are granted `oms` in the Jivo Auth admin.
- [ ] The [smoke test](#check-your-integration-against-production) passes
      from your production server.
- [ ] `API_KEY`, if used, is in your secret store and never in frontend
      code or Git.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `jivo_auth.E003` on `check` | `APP` isn't set. Every request would be refused. |
| `jivo_auth.E001` | A misspelled key in `JIVO_AUTH`. |
| Every request: 401 `Invalid token.` | Your `URL` doesn't match the token's `iss` (`http` vs `https`, a trailing path). Or the public key can't be fetched: look for `Could not fetch Jivo Auth signing keys` in your logs. |
| 401 `Token has expired.` straight after login | Server clock off by more than 10 seconds. |
| 403 for someone who should have access | Not granted in the Jivo Auth admin yet, or granted after their token was issued. They get it within 15 minutes, or at once by logging in again. |
| Browser: `blocked by CORS policy` on `auth.jivo.in` | Your frontend's origin isn't in Jivo Auth's `CORS_ALLOWED_ORIGINS`: the preflight answers 200 without `Access-Control-Allow-Origin`. Ask the Jivo Auth admin to add it. |
| Login from a script works, from the browser it doesn't | The same CORS cause as above: only browsers enforce CORS. |
| `403 Registration is disabled.` | Expected on production: ask an administrator to create the account. |
| Password reset or verification email never arrives | Email delivery isn't configured on production yet. An administrator can reset the password in the admin panel. |
| Browser: `blocked by CORS policy` on your API | Configure `django-cors-headers` in your API. |
| Users are logged out unexpectedly | The frontend isn't saving the new refresh token after each refresh, or two tabs refresh with the same token more than 30 seconds apart. Share tokens between tabs through `localStorage`, as in the example. |
| `IsAdminUser` always denies | `JivoUser.is_staff` is always `False`. Use local users, or your own permission class. |

## Settings reference

The `JIVO_AUTH` keys relevant to a DRF API. Each can also be set as a
`JIVO_AUTH_<KEY>` environment variable.

| Key | Default | Meaning |
|---|---|---|
| `URL` | — | `https://auth.jivo.in`. Sets the expected issuer and where the public key is fetched from. |
| `APP` | — | Your slug. Tokens without it in `apps` get 403. |
| `ALLOW_ALL_USERS` | `False` | With no `APP`, accept every Jivo user instead of refusing everyone. |
| `LOCAL_USERS` | `False` | `request.user` is a row of your user model instead of a `JivoUser`. |
| `LOCAL_USER_ID_FIELD` | `"username"` | Field of your user model holding the Jivo ID. |
| `LOCAL_USER_LINK_BY_EMAIL` | `False` | (0.3.0+) A user's first token links the one local row with the same email and no Jivo ID, instead of creating a new row. For applications whose users existed before Jivo Auth. |
| `API_KEY` | — | For `AuthClient().get_users()`. |
| `LEEWAY` | `10` | Seconds of clock drift tolerated. |
| `TIMEOUT` | `5` | Seconds to wait when fetching keys or calling Jivo Auth. |
| `PUBLIC_KEY` | — | PEM public key to verify with instead of fetching one. Tests, or fully offline servers; must then be updated by hand if Jivo Auth's key changes. |
| `ISSUER` | `URL` | Expected `iss`. `""` disables the check (not recommended). |
| `JWKS_URL` | `URL` + `/.well-known/jwks.json` | Override where keys are fetched from. |
| `ALGORITHM` / `SECRET` | `RS256` / — | Only if Jivo Auth is switched to a shared HS256 secret. |
