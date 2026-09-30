# Integrating a Django application

This guide adds Jivo login to a Django application, using the
`jivo-auth-client` package (import name `jivo_auth`) against
**https://auth.jivo.in**. The examples use an application with the slug
`oms` running at `https://oms.jivo.in`.

**Contents**

- [Before you start](#before-you-start)
- [1. Install the package](#1-install-the-package)
- [2. Configure it](#2-configure-it)
- [3. Pick how users log in](#3-pick-how-users-log-in)
  - [A. REST API (DRF) used by a frontend or mobile app](#a-rest-api-drf-used-by-a-frontend-or-mobile-app)
  - [B. Server-rendered Django site (templates, admin)](#b-server-rendered-django-site-templates-admin)
- [4. Working with users](#4-working-with-users)
- [5. Testing your application](#5-testing-your-application)
- [Troubleshooting](#troubleshooting)
- [Settings reference](#settings-reference)

## Before you start

- **Versions:** Python 3.10+ and Django 4.2+. DRF and PyJWT are installed
  with the package.
- **Slug and API key:** ask a Jivo Auth administrator to
  [register your application](register-an-application.md). You'll get:
  - the **slug**, e.g. `oms`
  - an **API key** (`jivo_...`). Keep it in your secret store. Only your
    server may use it.
- **Test user:** ask the administrator to grant a test user access to your
  application.

## 1. Install the package

The package lives in the private `Nareshkumar124/jivo-auth` repository. The
machine that installs it needs read access to that repository, through
your Git credentials, an SSH key, or a deploy key.

With uv:

```bash
uv add "jivo-auth-client @ git+https://github.com/Nareshkumar124/jivo-auth.git#subdirectory=packages/jivo-auth-client" --rev <commit-sha>
```

With pip, as a `requirements.txt` line:

```text
jivo-auth-client @ git+https://github.com/Nareshkumar124/jivo-auth.git@<commit-sha>#subdirectory=packages/jivo-auth-client
```

**Pin a commit or tag** instead of tracking `main`, so a change to Jivo Auth
never reaches your application unannounced.

**Docker builds** have no Git credentials of their own. Either pass a
read-only token as a build secret, or build a wheel beforehand
(`uv build packages/jivo-auth-client` in the jivo-auth repository) and
install that file.

## 2. Configure it

Add the app and a `JIVO_AUTH` block to your settings. Every key can also
come from an environment variable named `JIVO_AUTH_<KEY>`, e.g.
`JIVO_AUTH_API_KEY`.

```python
# settings.py
import os

INSTALLED_APPS = [
    # ...
    "rest_framework",
    "jivo_auth",
]

JIVO_AUTH = {
    "URL": "https://auth.jivo.in",
    "APP": "oms",                                   # your slug
    "API_KEY": os.environ["JIVO_AUTH_API_KEY"],     # from your secret store
    # Behind Nginx (or another proxy that sets X-Forwarded-For):
    "NUM_PROXIES": 1,
}
```

That's all most applications need. On startup, `python manage.py check`
reports mistakes such as a missing `APP`, a misspelled setting, or no way
to verify tokens. See [Troubleshooting](#troubleshooting).

Without `APP`, **every user is refused**. That's deliberate, so a
forgotten setting can't let every Jivo account in. If your application
really is for every Jivo user, say so explicitly with
`"ALLOW_ALL_USERS": True`.

## 3. Pick how users log in

| Your application | Use | `request.user` is |
|---|---|---|
| A REST API called by a React/Vue app, a mobile app or another service, with a token in each request | [A. DRF authentication](#a-rest-api-drf-used-by-a-frontend-or-mobile-app) | A `JivoUser` (no database), or your own `User` model with `LOCAL_USERS` |
| Django templates, `@login_required`, or the Django admin, with a login form and a session cookie | [B. Session login](#b-server-rendered-django-site-templates-admin) | Your own `User` model |

You can use both in the same project, for example an API for a mobile app
plus the Django admin.

### A. REST API (DRF) used by a frontend or mobile app

> For a DRF-only project, [Integrating a DRF API](integrate-drf.md) covers
> this in more depth: user modes, roles, a complete frontend client,
> testing and a go-live checklist.

**Server side.** Make Jivo tokens your API's authentication:

```python
# settings.py
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "jivo_auth.authentication.JivoJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}
```

Every request must send `Authorization: Bearer <access token>`. The
package verifies it locally:

- **Signature:** checked against the key from
  `https://auth.jivo.in/.well-known/jwks.json`, fetched once and cached.
- **Expiry:** checked with 10 seconds of leeway for clock differences.
- **Issuer and token type:** `iss` must be Jivo Auth and `token_type` must
  be `access`.
- **Access:** your slug must be in `apps`.

Failures come back as:

| Status | `detail` | Meaning |
|---|---|---|
| 401 | `Authentication credentials were not provided.` | No token sent. |
| 401 | `Token has expired.` | Refresh it and retry. |
| 401 | `Invalid token.` or `Access token required.` | Bad or forged token, or a refresh token sent as an access token. |
| 403 | `Your account does not have access to this application.` | The user isn't granted your slug. Refreshing won't help. |

In a view, `request.user` is a `JivoUser`:

```python
from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(["GET"])
def whoami(request):
    user = request.user            # jivo_auth.users.JivoUser
    return Response({
        "id": str(user.id),        # uuid.UUID, the token's `sub`
        "email": user.email,
        "apps": user.apps,         # ("oms", ...)
    })
```

A `JivoUser` has no database row, staff status or permissions: `is_staff`
is `False` and `has_perm()` is always `False`. If you need foreign keys to
users or per-user permissions in your application, turn on
[local users](#local-user-records).

If you use drf-spectacular, the package registers a `jivoAuth` bearer
scheme, so your Swagger UI gets an **Authorize** button.

**Client side (frontend or mobile app).** The client talks to Jivo Auth
for login, refresh and logout, and sends the access token to your API. Ask
the Jivo Auth administrator to allow your web origin for
[CORS](register-an-application.md#3-if-the-applications-frontend-calls-jivo-auth-from-the-browser).

```js
const AUTH = "https://auth.jivo.in/api/v1";

async function post(path, body) {
  const response = await fetch(AUTH + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return { status: response.status, data: await response.json() };
}

// Log in: returns { access, refresh }.
const { status, data } = await post("/auth/login/", {
  email, password, device_name: "OMS web",
});
// 401 with data.code === "email_not_verified": ask the user to open the
//   verification email (resend: POST /users/resend-verification/ { email }).
// 401 otherwise: wrong email or password.
// 429: too many attempts; wait a minute.

// Refresh when your API answers 401 (or shortly before `exp`). Store BOTH
// tokens from the response: the refresh token changes every time.
const refreshed = await post("/auth/refresh/", { refresh: tokens.refresh });
// 401 here means the session is over (logged out, password changed,
// deactivated): send the user to the login screen.

// Log out: ends this device's session. Works even after the access
// token has expired.
await post("/auth/logout/", { refresh: tokens.refresh });
```

Notes for client code:

- **The refresh token rotates.** Every refresh returns a new refresh
  token, and the old one stops working 30 seconds later. Sending an old
  one after that is treated as a stolen token and ends the session.
- **Checking access up front.** To show "no access" before calling your
  API, decode the access token's payload and check that `apps` contains
  your slug.
- **Profile, password and sessions** live at Jivo Auth: `GET/PATCH
  /users/me/`, `POST /users/change-password/`, `GET /auth/sessions/`. They
  use the same access token. See the
  [Swagger reference](https://auth.jivo.in/api/docs/).
- **Forgotten passwords:** link to `https://auth.jivo.in/forgot-password/`,
  or call `POST /users/forgot-password/ { email }` from your own form. The
  emailed link opens a reset page hosted by Jivo Auth, so you don't need to
  build one.

### B. Server-rendered Django site (templates, admin)

Users type their Jivo email and password into your site's login form. Your
server checks them with Jivo Auth, then logs the user in with a normal
Django session. `@login_required`, `request.user`, `LoginView` and the
Django admin all work as usual.

```python
# settings.py
AUTHENTICATION_BACKENDS = [
    "jivo_auth.backends.JivoAuthBackend",
    # Optional: local-only accounts, e.g. an emergency superuser.
    "django.contrib.auth.backends.ModelBackend",
]

MIDDLEWARE = [
    # ...
    "django.contrib.sessions.middleware.SessionMiddleware",
    # ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "jivo_auth.middleware.JivoSessionMiddleware",   # after AuthenticationMiddleware
    # ...
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "login"
```

```python
# urls.py
from django.contrib.auth import views as auth_views
from django.urls import path

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # ...
]
```

```html
<!-- templates/registration/login.html -->
<form method="post">
  {% csrf_token %}
  {{ form.non_field_errors }}
  <label>Email <input name="username" type="email" autocomplete="username" required></label>
  <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
  <button type="submit">Log in</button>
</form>
<a href="https://auth.jivo.in/forgot-password/">Forgot your password?</a>
<a href="https://auth.jivo.in/verify-email/">Didn't get the verification email?</a>
```

Notes on the login page:

- **Field name.** Django's form calls the field `username`; the backend
  treats it as the email. The Django admin's login form works the same way:
  the user types their Jivo email into **Username**.
- **Both links** go to pages hosted by Jivo Auth. They send the password
  reset or verification email, and the emailed links open Jivo Auth's pages
  too, so your site needs no pages of its own for either flow.
- **Logging out** must be a POST, which is what Django's `LogoutView`
  requires.

What happens behind the scenes:

1. **Login.** `JivoAuthBackend` sends the email and password to Jivo Auth.
   It sends your API key, the user's IP and their browser's User-Agent
   along, so rate limits apply per user and not to your server as a whole.
2. **Local record.** If the credentials are right and the user has access
   to your slug, it finds or creates their local `User` row:
   - `username` is set to the Jivo user ID and `email` to their email.
   - The password is unusable.
3. **Session.** The Jivo tokens are kept in the Django session.
4. **Refresh and revocation.** `JivoSessionMiddleware` refreshes the
   tokens shortly before they expire. If Jivo Auth refuses, the user is
   logged out of your site on their next request. That happens when they
   logged out elsewhere, changed their password, were deactivated, or lost
   access to your application.
5. **Logout.** Logging out of your site also ends the Jivo Auth session.

If Jivo Auth can't be reached, logged-in users stay logged in, and new
logins fail with the usual "wrong email or password" message. The error is
logged under `jivo_auth`.

Recommendations:

- **Use a shared cache** (`DatabaseCache`, Redis or Memcached) when you
  run several processes, so concurrent requests of one user share one token
  refresh. The default per-process cache still works; it just makes a few
  extra refreshes.
- **Admin access.** Staff status and permissions are local to your
  application. To make someone an admin of your site, have them log in
  once, then:

  ```bash
  python manage.py shell -c "from django.contrib.auth.models import User; User.objects.filter(email='alice@jivo.in').update(is_staff=True, is_superuser=True)"
  ```

  The admin's login form then accepts their Jivo email and password.

## 4. Working with users

### Local user records

With session login (B), local records are always created. For the REST API
(A), turn them on so `request.user` is a real model instance:

```python
JIVO_AUTH = {
    # ...
    "LOCAL_USERS": True,
    # Field that stores the Jivo user ID. "username" suits Django's
    # default User model.
    "LOCAL_USER_ID_FIELD": "username",
}
```

Now you can use ordinary foreign keys:

```python
from django.conf import settings
from django.db import models

class Order(models.Model):
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
```

How local records behave:

- **Email:** the record's email follows the token.
- **Deactivating:** deactivating the record locally (`is_active=False`)
  blocks that user in your application only.
- **Custom user model:** point `LOCAL_USER_ID_FIELD` at any unique field of
  your model, such as a `jivo_id = models.UUIDField(unique=True)`, or at
  `"id"` if your model's primary key is a UUID and should equal the Jivo
  ID.

**Moving an existing application onto Jivo Auth:** existing rows aren't
matched automatically. Link them once by email, before users start logging
in through Jivo:

```python
from django.contrib.auth.models import User
from jivo_auth.client import AuthClient

jivo_ids = {u["email"]: u["id"] for u in AuthClient().get_users()}

for user in User.objects.exclude(email=""):
    jivo_id = jivo_ids.get(user.email.lower())
    if jivo_id:
        user.username = jivo_id
        user.save(update_fields=["username"])
```

### Looking users up

Your server can list or look up the users who have access to your
application. This uses the API key, so it works in background jobs too:

```python
from jivo_auth.client import AuthClient

client = AuthClient()

everyone = client.get_users()        # all users with access
some = client.get_users(jivo_ids)    # only these Jivo user IDs; batched for you
# [{"id": "...", "email": "...", "first_name": "...", "last_name": "...", "is_active": True}]
```

Users without access to your application are never returned.

### Calling other Jivo APIs as the user

In session mode, the user's current access token is in the session:

```python
from jivo_auth.session import get_access_token

token = get_access_token(request)   # None for users who didn't log in through Jivo
headers = {"Authorization": f"Bearer {token}"}
```

With the REST API (A), the token the client sent is `request.auth`.

### Errors from the client

`AuthClient` raises `jivo_auth.exceptions.AuthServiceError`. It carries
`.status` and `.data`, the HTTP status and response body. When Jivo Auth
can't be reached it raises the subclass `AuthServiceUnavailable`, whose
`.status` is `None`.

## 5. Testing your application

Don't call auth.jivo.in from your tests.

**DRF views:** authenticate the test client directly:

```python
import uuid
from rest_framework.test import APITestCase
from jivo_auth.users import JivoUser

class OrderTests(APITestCase):
    def setUp(self):
        self.client.force_authenticate(
            JivoUser(id=uuid.uuid4(), email="tester@jivo.in", apps=("oms",))
        )
```

With `LOCAL_USERS`, create a `User` and pass that to `force_authenticate`
instead.

**Session views:** log a local user in without Jivo Auth:

```python
self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
```

The middleware leaves sessions without Jivo tokens alone. This needs
`ModelBackend` in `AUTHENTICATION_BACKENDS`.

**End-to-end tests of real tokens:**
1. Generate a key pair in your test setup.
2. Set `JIVO_AUTH["PUBLIC_KEY"]` to the public key with `override_settings`.
3. Sign tokens with PyJWT: RS256 with `sub`, `email`, `apps`, `iss`,
   `token_type`, `exp` and `iat`.

## Troubleshooting

`python manage.py check` reports these:

| Check | Problem | Fix |
|---|---|---|
| `jivo_auth.E001` | Unknown key in `JIVO_AUTH` | Fix the spelling. See the [reference](#settings-reference). |
| `jivo_auth.E002` | No way to verify tokens | Set `URL` (or `PUBLIC_KEY`, or `SECRET` for HS256). |
| `jivo_auth.E003` | `APP` not set, so every user is refused | Set your slug, or `ALLOW_ALL_USERS`. |
| `jivo_auth.W001` | `ALLOW_ALL_USERS` lets every Jivo user in | Intended? Otherwise set `APP`. |
| `jivo_auth.W002` | Session login without `API_KEY` | All logins count against your server's IP, so a few wrong passwords block everyone for a minute. Set `API_KEY`. |

At runtime:

| Symptom | Likely cause |
|---|---|
| Every token gives 401 `Invalid token.` | `URL` doesn't match the token's `iss` (e.g. `http` vs `https`, or a trailing path), or your server can't reach `/.well-known/jwks.json` on first use. Check the `jivo_auth` warnings in your logs. |
| 401 `Token has expired.` right after login | Your server's clock is off by more than 10 seconds. Enable NTP. |
| 403 for a user who should have access | Not granted in the Jivo Auth admin, or the grant is newer than their token. They get it on the next refresh, within 15 minutes, or by logging in again. |
| Login form always says the password is wrong | Check your logs for `jivo_auth` messages:<br>• `email not verified`: the user can get a new link at `https://auth.jivo.in/verify-email/`.<br>• `has no access to this application`: grant the user your slug in the Jivo Auth admin.<br>• No message: the email or password really is wrong.<br>• `Jivo Auth login failed`: Jivo Auth refused or couldn't be reached. |
| Everyone gets "too many attempts" at once | `API_KEY` or `NUM_PROXIES` isn't set, so Jivo Auth sees all logins as coming from one IP. |
| Users are logged out at random | Several processes with a per-process cache, plus very slow responses from Jivo Auth. Use a shared cache. |

## Settings reference

All keys go in `JIVO_AUTH` (or `JIVO_AUTH_<KEY>` environment variables).

| Key | Default | Meaning |
|---|---|---|
| `URL` | — | `https://auth.jivo.in`. Default for the issuer, the key URL and API calls. |
| `APP` | — | Your application's slug. Tokens without it in `apps` get 403. |
| `ALLOW_ALL_USERS` | `False` | With no `APP`, accept every Jivo user instead of refusing everyone. |
| `API_KEY` | — | Your application's API key, for session login and user lookups. |
| `NUM_PROXIES` | `0` | Proxies in front of your application that append to `X-Forwarded-For`. `0` means use `REMOTE_ADDR`; set `1` behind Nginx. |
| `ISSUER` | `URL` | Required `iss` claim. `""` disables the check. |
| `JWKS_URL` | `URL` + `/.well-known/jwks.json` | Where the public keys are fetched from. |
| `PUBLIC_KEY` | — | PEM public key. Verifies without fetching keys. The key must then be updated by hand when Jivo Auth's key changes. |
| `ALGORITHM` | `RS256` | `HS256` only if Jivo Auth is configured for a shared secret. |
| `SECRET` | — | The shared secret for `HS256`. |
| `LEEWAY` | `10` | Seconds of clock difference tolerated. |
| `TIMEOUT` | `5` | Seconds to wait for Jivo Auth. |
| `LOCAL_USERS` | `False` | DRF: return local `User` records instead of `JivoUser`. |
| `LOCAL_USER_ID_FIELD` | `"username"` | Field of your user model holding the Jivo user ID. |
