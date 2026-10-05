# Phase 4: Switch the app to Jivo Auth

One release: the backend and frontend deploy together, after the final
re-run of the migration (migrate-users.md, step 7). Tokens from the old
system stop working, so everyone signs in again. Tell the user to announce it.

## Settings

```python
# settings.py
INSTALLED_APPS += ["jivo_auth"]          # if phase 2 didn't add it

REST_FRAMEWORK = {
    # ...
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "jivo_auth.authentication.JivoJWTAuthentication",
        # Only if the browsable API or session-based pages must keep working:
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",   # keep the app's existing default
    ],
}

JIVO_AUTH = {
    # URL, APP, API_KEY and NUM_PROXIES come from JIVO_AUTH_URL, JIVO_AUTH_APP,
    # JIVO_AUTH_API_KEY and JIVO_AUTH_NUM_PROXIES in the environment.
    "LOCAL_USERS": True,                  # request.user is the app's own User row
    "LOCAL_USER_ID_FIELD": "auth_id",
    "LOCAL_USER_LINK_BY_EMAIL": True,     # decision D4
}

# The Django admin (and any template pages): Jivo email + password, nothing else.
AUTHENTICATION_BACKENDS = [
    "jivo_auth.backends.JivoAuthBackend",
    # Decision D7 only, from templates/break_glass_backend.py:
    # "<app>.backends.BreakGlassBackend",
]
# BREAK_GLASS_USERNAMES = ["breakglass"]
```

**Don't keep `django.contrib.auth.backends.ModelBackend`.** Until cleanup,
every migrated row still holds its old password hash, and ModelBackend would
let anyone sign in to the admin with their old password. For an emergency
local account, copy `templates/break_glass_backend.py`: it accepts local
passwords only for the usernames in `BREAK_GLASS_USERNAMES`.

```python

MIDDLEWARE = [
    # ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "jivo_auth.middleware.JivoSessionMiddleware",   # right after AuthenticationMiddleware
    # ...
]

CACHES = ...   # a shared cache (database, Redis) when several processes run: the session middleware serializes refreshes through it.
               # With no CACHES at all, each process has its own memory cache: fine for one runserver, not for several workers.
```

The environment variables must actually reach the process. If the app
doesn't load `.env` yet (`python-dotenv`, or the deployment's environment),
add that as well. If it reads `.env` through `python-decouple` or
`django-environ`, which don't fill `os.environ`, pass the four values
explicitly in `JIVO_AUTH` (analysis.md, section 1). `python manage.py check`
reports `jivo_auth.E002`/`E003` when `URL`/`APP` are missing.

**A dev server reloads mid-edit.** If it hits an `ImportError` while files
are half-changed, Django's reloader then waits for the next file change and
the port stays closed. `touch` a settings file to restart it.

Then remove the old token settings (`SIMPLE_JWT`, `REST_KNOX`, `DJOSER`, `REST_AUTH`, ...). Keep their apps in `INSTALLED_APPS` until cleanup, because their tables are dropped there.

What the client does with each request:

- It verifies the signature (JWKS by `kid`, cached), the expiry (with 10 s leeway), the issuer, `token_type == "access"`, and that the slug is in `apps`. A missing slug is a 403.
- It finds the local user by `auth_id`, or links a row by email (D4), or creates one. The local `is_active=False` gives a 401.
- `request.user.jivo_claims` holds the token's claims, and `request.auth` holds the raw token.

## Remove the old authentication

These come from the analysis, section 5.

- **Endpoints:** sign-in, token obtain/refresh/verify, sign-out, registration, password reset/change, email verification and OTP. That covers URL entries, views and serializers.
  - For mobile apps that can't update on the same day, a tiny view on the old sign-in URL can answer `410 Gone` with a message saying where to sign in now. Give it `authentication_classes = []` and `AllowAny`, so a client still sending an old token gets the 410 rather than a 401 it would try to refresh:
    ```python
    class LoginGoneView(APIView):
        permission_classes = [AllowAny]
        authentication_classes = []

        def post(self, request, *args, **kwargs):
            return Response({"detail": "Sign in at Jivo Auth.",
                             "sign_in_url": "https://auth.jivo.in/api/v1/auth/login/"}, status=410)
        get = post
    ```
- **Signals and middleware** of the old system, e.g. `post_save` creating a DRF `Token`, or custom JWT middleware. Middleware that decodes tokens itself to identify the actor (audit or request logging) switches to `JivoJWTAuthentication().authenticate(request)`, so it resolves the same row the view sees. Wrap it: it raises `AuthenticationFailed` or `PermissionDenied` for a bad token, which outside a DRF view is a 500. On `rest_framework.exceptions.APIException`, treat the request as anonymous.
- **Custom authentication classes.**
- **Code that sets or checks passwords.** The user's password lives in Jivo Auth now. A step-up confirmation ("type your password again before X", decision D9) becomes a fresh Jivo sign-in checked with [templates/fresh_sign_in.py](../templates/fresh_sign_in.py). Don't accept "an access token issued in the last N seconds" as proof: a refresh mints one too, without the password, from the refresh token any signed-in browser holds. The template checks the Jivo Auth *session* the password sign-in created, then revokes it so it confirms once.
- **Healthchecks and scripts that hit the old sign-in URL** (Dockerfile `HEALTHCHECK`, compose, monitors): point them at a health endpoint.
- **Don't remove** permission classes, role checks, object-ownership rules or `IsAdminUser`. They keep working on the local row.

## User endpoints and serializers

- **Remove `password`, `confirm_password` and `new_password` fields everywhere.**
- **Make Jivo-owned fields read-only:** `email`, `first_name`, `last_name` (and an employee code). Users change their names at Jivo Auth: `PATCH https://auth.jivo.in/api/v1/users/me/`. Only administrators change emails, in the Jivo Auth admin.
- **Expose `auth_id` read-only** where clients need it.
- **Keep every app-specific field** editable exactly as before.

### Creating users

Accounts are created in Jivo Auth: Admin → Users → Add, then grant the slug under **Application access**. An endpoint or admin screen that used to create a user with a password becomes one of these:

1. **Nothing to do:** the local row is created at the user's first sign-in, with an unusable password.
2. **Set app data before first sign-in:** run `python manage.py sync_jivo_users --create-missing`, or replace the old create endpoint with an "add a Jivo user to this app" endpoint:

```python
from django.contrib.auth import get_user_model
from rest_framework.exceptions import APIException, ValidationError
from jivo_auth.client import AuthClient
from jivo_auth.exceptions import AuthServiceError
from jivo_auth.users import get_local_user


class JivoUnavailable(APIException):
    status_code = 503
    default_detail = "Jivo Auth can't be reached. Try again shortly."


def add_jivo_user(auth_id):
    """(user, created): the local row for a Jivo user who has access to this app."""
    User = get_user_model()
    try:
        matches = AuthClient().get_users([auth_id])  # only users granted this app come back; needs the API key
    except AuthServiceError as exc:
        raise JivoUnavailable() from exc
    if not matches:
        raise ValidationError({"auth_id": "No Jivo user with access to this application has that ID."})
    jivo = matches[0]
    existing = User.objects.filter(auth_id=jivo["id"]).first()
    if existing:
        return existing, False
    # With LOCAL_USER_LINK_BY_EMAIL, get_local_user() would quietly link an unlinked
    # row with this email, often an inactive user the import skipped, and the
    # caller's fields would overwrite that person's. Make it explicit instead:
    # edit that row; it is linked at the person's first sign-in.
    same_email = User.objects.filter(auth_id__isnull=True, email__iexact=jivo["email"]).first()
    if same_email:
        raise ValidationError({"auth_id": f"Local user {same_email.pk} already has this email; edit that user."})
    user = get_local_user({"sub": jivo["id"], "email": jivo["email"]})
    # Copy the Jivo-owned fields the model keeps (names), then set the app's own fields as before.
    return user, True
```

Run `add_jivo_user()` and the writes of the app's own fields in one `transaction.atomic()`: a failed field write must not leave a half-made row behind, which the next attempt would then refuse as "already in the app".

To pick people by email, list `AuthClient().get_users()` (everyone with access). An admin-only endpoint that returns each one with the local row it already has (`{auth_id, email, name, is_active, local_user_id}`, `local_user_id` null when none) lets the create form offer exactly the people still to add. Answer 503 when Jivo Auth can't be reached (`AuthServiceError`, of which `AuthServiceUnavailable` is a subclass). Apps often have more than one such screen, e.g. a module's own user admin; every one changes the same way.

## The Django admin

For the user model's `ModelAdmin`:

- **Drop the password parts:** `password` from fieldsets, the "change password" link, and `UserCreationForm`/`add_fieldsets`.
- **Close the password URL.** `django.contrib.auth.admin.UserAdmin` still serves `<id>/password/`, which sets a local password, even with the link gone. Override `user_change_password` to `raise Http404`, or base the admin on `ModelAdmin` instead. With `ModelAdmin` the URL redirects (`<id>/password/` matches the catch-all "change" redirect) rather than 404s; either is fine, as long as no password form is served.
- **Add `auth_id`** to `readonly_fields` and `list_display`. A filter "Linked to Jivo" (`auth_id__isnull`) helps in the transition.
- **Make Jivo-owned fields read-only:** `email`, `first_name`, `last_name`.
- **Adding users:** either `has_add_permission` returns `False`, with a note to create accounts in Jivo Auth, or an add form that takes a Jivo user's ID or email and calls `add_jivo_user()`.
- **Keep** `is_active`, `is_staff`, `is_superuser`, groups, permissions and every app field editable.
- **Sign-in:** staff sign in to the admin with their **Jivo email** in the "Username" box. To relabel it, set `admin.site.login_form` to an `AdminAuthenticationForm` subclass whose `username` field is labelled "Email".
- **Staff access:** a migrated staff user keeps `is_staff`/`is_superuser` on the local row, so the admin works for them after they sign in with Jivo.

## Other code

- **Management commands, fixtures and seed scripts** that create users with passwords: create them without one (`set_unusable_password()`), with `auth_id` set when it's known.
- **Code reading removed fields** (`email_verified` and so on): Jivo only issues tokens to verified, active users, so such checks can go.
- **Anything calling another Jivo API as the user:** forward `request.auth`, which is the user's own access token.

## Tests

- **Tests using `force_authenticate(user)`** with a local user keep working as they are. For session tests, use `force_login(user, backend="jivo_auth.backends.JivoAuthBackend")`: `ModelBackend` is no longer listed, and the session middleware leaves sessions without Jivo tokens alone.
- **Tests that signed in through the removed endpoints:** replace them with `force_authenticate`, or with real tokens signed by a test key. See [templates/jivo_test_tokens.py](../templates/jivo_test_tokens.py). Tests that pinned the old login response, its rate limit, or password validation on user creation become tests of the new contract (410/404, "the app ignores password, name and email").
- **Add tests for these** (most are in [templates/test_jivo_auth.py](../templates/test_jivo_auth.py); adapt its `ADAPT` lines):
  - a token for a user with `auth_id` resolves to that same row, with the same primary key and the same business records;
  - a token without the slug gets 403; expired, foreign-signed and refresh-type tokens get 401;
  - a locally deactivated user gets 401;
  - a first sign-in with an unmapped email links the row (D4 on) or creates a new one (D4 off), and never claims a row that's already linked;
  - the old sign-in URLs are gone (404, or 410);
  - an old local password no longer opens the admin, `<id>/password/` serves no password form, and the admin can't create a user with a password (no add form, or one that adds a Jivo user);
  - the "add a Jivo user" endpoints refuse someone already in the app, someone with an unlinked same-email row, and answer 503 when Jivo Auth is down;
  - a step-up confirmation (D9) refuses an ordinary or stale session, another user's token, and a second use.
- **Admin pages in tests and manifest static storage:** whitenoise's or Django's `ManifestStaticFilesStorage` needs `collectstatic`, which tests don't run, so rendering an admin page fails with "Missing staticfiles manifest entry". Override `STORAGES["staticfiles"]` with `StaticFilesStorage` on those test classes.
- **Compare against the baseline** (analysis.md, section 7): the same failing tests as before, and no new ones.
- **Don't call auth.jivo.in from the test suite.**

## Documentation and configuration

- **`.env.example`:** add `JIVO_AUTH_URL`, `JIVO_AUTH_APP`, `JIVO_AUTH_API_KEY` and `JIVO_AUTH_NUM_PROXIES` without values, and remove the old token settings.
- **README and API docs:** clients now sign in at `https://auth.jivo.in/api/v1/auth/login/`, and this API takes `Authorization: Bearer <Jivo access token>`.
- **Run `python manage.py check`.** It must be clean: `jivo_auth.E003` means `APP` isn't set, and `W002` means the admin sign-in has no API key.

## Deploy

1. **Take a backup**, then do the final migration re-run (migrate-users.md, step 7).
2. **Get confirmation** (⛔), then deploy the backend and frontend together.
3. **Run the phase 5 verification** from SKILL.md straight away.

## Rollback

- **Before cleanup, rollback is a redeploy of the previous release** (backend and frontend). Nothing that release needs was removed:
  - old password hashes are still in the rows;
  - the old token tables still exist;
  - `auth_id` is just an extra column.
- **Accounts created in Jivo Auth stay there.** They're harmless: without the slug in use they grant nothing.
- **After cleanup**, rolling back needs the database backup taken before cleanup.
