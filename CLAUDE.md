# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Jivo Auth is a central JWT authentication service (Django 6.1 + DRF + SimpleJWT, PostgreSQL), deployed at `https://auth.jivo.in`. It issues RS256 access/refresh tokens (HS256 remains available via `JWT_ALGORITHM`). Other Jivo apps (Django, FastAPI, Node) verify those tokens locally with the public key from `/.well-known/jwks.json` instead of calling this service on every request. Redis is deliberately not used in V1.

`docs/` holds the integration guides for consuming applications and the deployment guide; update them when settings, endpoints or client behaviour change. `skills/integrate-jivo-auth/` is a Claude Code skill that moves an existing DRF application onto Jivo Auth (prerequisites, analysis, `auth_id`, `import_users`, switch, cleanup); keep it in step with the client package and `import_users`, and keep it free of server details, since the repository is public. `README.md` is the phased roadmap (Phases 1–10; the MVP is Phases 1–4). Work proceeds one phase at a time. When README and code disagree on routes, the code is correct. For example, register and the password endpoints live under `/api/v1/users/`, not `/api/v1/auth/`, and sessions are revoked with `POST .../revoke/` rather than `DELETE`.

## Commands

Dependencies are managed with `uv` (Python 3.13, per `.python-version`). `packages/jivo-auth-client` is a uv workspace member, and it is installed editable in the dev group so the service's tests can exercise it.

```bash
uv sync                                   # install deps, including the dev group
uv run python manage.py runserver
uv run python manage.py makemigrations && uv run python manage.py migrate

# Tests (need a reachable Postgres; the runner creates and drops test_<POSTGRES_DB>)
uv run python manage.py test
uv run python manage.py test authentication.tests.RefreshTests.test_refresh_rejects_rotated_token
uv run pytest                             # configured in pyproject.toml
uv run pytest users/tests.py::ResetPasswordTests
uv run pytest packages/jivo-auth-client    # client package: its own settings, SQLite, no Postgres

uv run python manage.py spectacular --validate --fail-on-warn --file /dev/null   # schema check

./deploy.sh                               # redeploy this working tree to auth.jivo.in (--dry-run, --rollback; target in gitignored deploy.env; see docs/deploy-auth-service.md)
docker compose up -d --build              # the Django app only, on host networking: uses the host's PostgreSQL at 127.0.0.1:5432, serves on 127.0.0.1:8000

uv run python -m config.jwt_keys > jwt_private.pem   # new RSA signing key (gitignored)
uv run python manage.py app_api_key oms --create "Jivo OMS"   # create/rotate an application's API key
uv run python manage.py import_users oms users.json > mapping.json   # bring in an application's existing users (--dry-run, --mark-verified)
```

Settings come from `.env`, loaded by `python-dotenv` in `config/settings.py`; `.env.example` lists every variable with its production value. `DEBUG` defaults to off, and startup fails with `ImproperlyConfigured` when the JWT key is missing or, with `DEBUG` off, when `DJANGO_SECRET_KEY` or an `https://` `PUBLIC_URL` is missing. The default cache is a Postgres `DatabaseCache` (table created by `applications/migrations/0002_cache_table.py`), so throttle counters are shared by all workers. Logs go to stderr (`LOGGING`). Postgres is the only configured database, and there is no SQLite fallback. PostgreSQL runs on the host, never in Docker: `runserver` and the container use the same `POSTGRES_HOST=127.0.0.1`.

**Docker.** Only the Django app is containerized. `dockerfile` is multi-stage: uv installs the production dependencies, then the runtime image runs as uid 10001 and collects static files (served by WhiteNoise) with throwaway build-time secrets. `docker/entrypoint.sh` migrates (unless `RUN_MIGRATIONS=0`) and starts gunicorn on `GUNICORN_BIND` (with `--no-control-socket`: gunicorn 25.1+ would otherwise try to create it in the read-only `$HOME`, `/app`), and `docker/healthcheck.py` is the `HEALTHCHECK`. `docker-compose.yml` uses `network_mode: host` and mounts `jwt_private.pem` as a secret, which keeps its file permissions, so `user:` comes from `APP_UID`/`APP_GID`; it also caps the json-file logs at 5 × 10 MB. `.dockerignore` keeps `.env`, `.venv`, `*.pem` and `docs/` out of the image.

Email goes through Django 6.1 `MAILERS`, built from the `EMAIL_BACKEND` (`smtp`/`console`) and `EMAIL_*` env vars. Don't define legacy `EMAIL_*` settings: Django 6.1 rejects them next to `MAILERS`.

There is no linter or formatter configured.

## Architecture

**Django project `config/`** has a single settings module. Every API route is mounted under `/api/v1/` in `config/urls.py`, except `/.well-known/jwks.json` and the HTML pages in `users/pages.py`: `/forgot-password/` (linked from applications' login pages) and the pages emailed links open (`/reset-password/`, `/verify-email/`, which also offers a resend form). Their POSTs use the API's throttle scopes via `ScopedRateThrottle().allow_request()`. Swagger UI is at `/api/docs/` and the schema at `/api/schema/` (drf-spectacular). DRF defaults are `IsAuthenticated` plus SimpleJWT `JWTAuthentication`, so any public view must opt out explicitly with `AllowAny` (or with empty `authentication_classes`/`permission_classes`, as `health` does).

**API docs.** Each app's `schema.py` holds its endpoints' `extend_schema` objects: tags, summary, description, responses and examples. The views only apply them as class decorators.
- **Shared pieces** are in `config/openapi.py`: `MessageSerializer`, `ErrorSerializer`, the 401 and 429 responses, example tokens, and `rate_limit()`, which reads the throttle settings.
- **Global components** live in `SPECTACULAR_SETTINGS["APPEND_COMPONENTS"]` in settings: the `jwtAuth` scheme and `ValidationError`. The `appKey` scheme comes from the `OpenApiAuthenticationExtension` in `applications/schema.py`.
- **Security** comes from the view's authentication and permission classes. Public views must pass `auth=[]`.
- **Don't add a global `SECURITY` setting.** It gets appended to every operation.
- **List endpoints:** give examples as a single item, because drf-spectacular wraps them in a list itself.
- **Test enforcement:** `APIDocsTests` in `health/tests.py` fails on any schema warning, on a missing tag, summary or description, on security that doesn't match `PUBLIC_OPERATIONS`/`APPLICATION_OPERATIONS`, or on an example that doesn't match its schema.

**Throttling.** The throttle classes are the subclasses in `applications/throttling.py`, and a view opts in by setting `throttle_scope` (`register`, `forgot_password`, `reset_password`, `verify_email`, `resend_verification`, `application`). A `scope` attribute on a `ScopedRateThrottle` subclass is ignored by DRF. `LoginView` instead sets `throttle_classes` to `LoginRateThrottle` (per account and IP, `login`) and `LoginIPRateThrottle` (per IP, `login_ip`), which are `SimpleRateThrottle`s with fixed scopes. Client identity (`get_client_ident()`/`get_client_ip()`, also used for session IPs) comes from `REST_FRAMEWORK["NUM_PROXIES"]`, set by the `NUM_PROXIES` env var and defaulting to 0. With 0, `X-Forwarded-For` is ignored. Set it to 1 behind Nginx. A request with a valid `X-Jivo-App-Key` may name the end user's IP in `X-Jivo-Client-IP` (apps logging users in from their server). Throttle counters live in the default (database) cache.

**`users/`** holds the custom `AUTH_USER_MODEL = "users.User"`. It has a UUID primary key, `email` as `USERNAME_FIELD`, and no username field.
- **Email handling.** Emails are case-insensitive everywhere. `UserManager.create_user` lowercases them, `get_by_natural_key` (used by login) matches with `iexact`, and a `Lower("email")` unique constraint backs this up. Use `email__iexact` for lookups.
- **Employee code.** `User.employee_code` is optional and unique when set (conditional `UniqueConstraint`, error code `employee_code_taken`, which `validate_constraints()` moves onto the field). It's normalized to uppercase in both `clean_fields()` (before validation) and `save()`. Administrators set it; the APIs expose it read-only (`/users/me/`, `/apps/users/`), and it isn't a token claim.
- **Endpoints.** The app owns registration, email verification, `GET`/`PATCH /users/me/` (only names are editable), and the password flows. `REGISTRATION_ENABLED` and `REGISTRATION_EMAIL_DOMAINS` gate registration; registering never grants application access.
- **Verified email.** With `REQUIRE_VERIFIED_EMAIL` (default on) login and refresh refuse unverified users with 401 `code: email_not_verified`, so the `email` claim can be trusted. `resend-verification` is public (by email). `create_superuser` and the admin add form mark users verified.
- **Password changes.** `User.save()` calls `end_sessions_after_password_change()` whenever `set_password()`/`set_unusable_password()` changed the password (API, admin, `changepassword`), revoking every session and pending reset token. Hash upgrades on login don't count (Django clears `_password` for them).
- **Flows live in `users/services.py`**, shared by the API views and the HTML pages. Views catch `InvalidTokenError` and return its message as a 400 `detail`.
- **Reset and verification tokens.** These are stored only as a SHA-256 hash (`PasswordResetToken`, 15 minutes; `EmailVerificationToken`, 24 hours) and emailed as links built from `PASSWORD_RESET_URL`/`EMAIL_VERIFICATION_URL` (`{token}` placeholder; defaults are this service's own pages). The forgot-password response is identical whether or not the account exists, and it includes `reset_token` only when `RETURN_RESET_TOKEN_IN_RESPONSE` (`DEBUG` on and the console email backend).
- **Importing users** (`users/management/commands/import_users.py`): an application's existing users, as JSON, become accounts granted its slug, in one atomic run. It keeps password hashes that one of `PASSWORD_HASHERS` can verify, links emails that already have an account without changing them, never imports staff flags, and prints `source_id` → Jivo ID. It sets `user._audit_source = "import"`, which `audit/signals.py` uses as the `ACCOUNT_CREATED` source.
- **Password rules.** `users/validators.py` runs `AUTH_PASSWORD_VALIDATORS` plus the composition rules. Passwords hash with Argon2, and the PBKDF2 hashers are kept so older hashes still verify.

**`applications/`** registers the Jivo apps that trust these tokens. An `Application` has a `slug`, an M2M `users` (access grants, edited as checkboxes in the user admin), and an API key stored only as a SHA-256 hash (`app_api_key` command, or the admin's "Rotate API key" button, which shows the key once on its own `no-store` page). `ApplicationKeyAuthentication` reads `X-Jivo-App-Key` for the server-to-server endpoint `GET /api/v1/apps/users/`, which only returns users with access to the calling app.

**`authentication/`** wraps SimpleJWT and adds device sessions. Most logic lives in serializers, not views:
- JWT claims: `sub` (the user UUID), `token_type`, `email`, `apps` (active application slugs, from `add_user_claims()` in `authentication/tokens.py`) and `iss` (`JWT_ISSUER`, default `PUBLIC_URL`).
- **Signing.** SimpleJWT has no hook for token headers, so `AuthenticationConfig.ready()` replaces `rest_framework_simplejwt.state.token_backend` with `KeyIdTokenBackend`, which adds the key's `kid` (its RFC 7638 thumbprint, from `config/jwt_keys.py`). `JWKSView` publishes `settings.JWT_PUBLIC_JWK`.
- Refresh tokens rotate, and the old one is blacklisted (`token_blacklist` app).
- `UserSession` maps a refresh token's `jti` to device info.
  - `LoginSerializer` creates the session.
  - `SessionTokenRefreshSerializer` refuses any refresh token without an active session or for a user who may not get tokens (`check_can_get_tokens()`). It locks the row with `select_for_update`, blacklists the old token and issues new ones with `issue_tokens()`, so claims follow the user's current email and access, then moves `refresh_jti` to the new token and keeps the old one in `previous_refresh_jti`.
  - **Reuse grace.** The previous token is accepted again for `REFRESH_TOKEN_REUSE_GRACE` (30s) and returns the current pair (the stored `OutstandingToken.token`; `issue_tokens()` rewrites it to include the claims). Reuse after that revokes the session.
  - `SessionTokenRefreshSerializer` must raise `TokenError`/`InvalidToken`, which become a 401, never a bare exception, which becomes a 500.
- Revoke sessions only through `authentication/services.py`: `revoke_sessions()` and `revoke_all_sessions()`. These blacklist the matching `OutstandingToken`s and set `revoked_at`. Any new token-issuing path must create a `UserSession`, or its refresh tokens will be rejected.
- `/auth/logout/` is public: the refresh token in the body is the proof, so logout works after the access token expires.
- `/auth/verify/` is SimpleJWT's `TokenVerifyView`.

**`audit/`** is the security log. Write events only through `audit.services.record(type, user=..., application=..., **details)`: it fills in the client IP, user agent and acting admin from the current request (`audit.context.AuditRequestMiddleware`, a contextvar). Account creation and application-access changes are recorded by signals in `audit/signals.py` (`m2m_changed` on `Application.users`, so change grants with `add`/`remove`/`set`, not through-model rows). `User.save()` records deactivation and reactivation; `end_sessions_after_password_change()` records password changes, or resets when `user._password_reset` is set. `AuditEvent.severity` and `.summary` drive the badges and one-line details; `prune_audit_events` deletes events older than `AUDIT_LOG_RETENTION_DAYS`.

**`adminpanel/`** is the redesigned Django admin.
- **Site.** `adminpanel.apps.JivoAdminConfig` replaces `django.contrib.admin` in `INSTALLED_APPS` and makes `JivoAdminSite` (`sites.py`) the default site, so `@admin.register` works as usual. The site adds the grouped sidebar (`NAV_SECTIONS`) and the dashboard (`dashboard.py`).
- **Templates and theme.** `adminpanel` comes first in `INSTALLED_APPS`, so its `templates/admin/` override Django's. The theme is `static/adminpanel/css/admin.css`: `--jv-*` tokens for light and dark, mapped onto Django's CSS variables. Behaviour is in `static/adminpanel/js/admin.js`. The CSS loads in `base_site.html`'s `responsive` block, the last in `<head>`, so it beats Django's own sheets. Django 6.1 underlines content links with a selector weighing (1,1,2); component rules use `#container #main a.x` to beat it.
- **Charts.** Plain HTML, with geometry from `charts.py`; the series colours were checked with the dataviz palette validator. The dashboard computes each section only if the viewer has that model's view permission.
- **Building blocks** (`admin_tools.py`): `badge()`, `relative_time()`, `titled()` (readable filter titles), `recent_filter()`, `describe_user_agent()`, and `confirm_action()`. Every destructive bulk action must go through `confirm_action()`: it renders `adminpanel/confirm_action.html` and runs on the confirming POST (`jv_confirm=yes`). Extra change-form buttons come from `change_view(extra_context={"object_tools": [...]})`, shown by `admin/change_form_object_tools.html`, and must POST to views that re-check permissions.
- **Access rules.**
  - Only superusers can change superusers, staff flags, groups or permissions (`UserAdmin.get_readonly_fields` and `has_change_permission`), or staff roles.
  - Staff roles are `StaffRole`, a proxy of `Group` with its own `adminpanel.*_staffrole` permissions.
  - The default roles (`roles.py`) are created after migrations only if missing, so edits to them are never overwritten.
  - SimpleJWT's token tables are hidden; revoke through Sessions.
- **Docs tab** (`/admin/docs/`, `JivoAdminSite.docs`, open to all staff): the DRF integration guide, `templates/adminpanel/docs/drf.html`. `docs.py` fills it from the running configuration (URLs, token lifetimes, rate limits, CORS, registration, email) and builds its API reference from the drf-spectacular schema, so new endpoints appear there without edits. Code samples are constants in `docs.py` with `__BASE__`/`__APP__`/`__ISSUER__` placeholders; keep them in step with `docs/integrate-drf.md` and the client package.
- **Django 6.1:** `format_html()` needs arguments; use `mark_safe()` for static snippets.

**`packages/jivo-auth-client/`** is an installable package (0.3.0) with the import name `jivo_auth`. It depends on Django, DRF and PyJWT (not SimpleJWT).
- **Config** is the `JIVO_AUTH` dict in the consuming app's settings, falling back to `JIVO_AUTH_<NAME>` env vars. `jivo_auth/settings.py` documents every key (`URL`, `APP`, `ALLOW_ALL_USERS`, `API_KEY`, `NUM_PROXIES`, `PUBLIC_KEY`, `SECRET`, `LEEWAY`, `LOCAL_USERS`, ...). System checks `jivo_auth.E001`-`E003`/`W001`/`W002` run when `jivo_auth` is in `INSTALLED_APPS`.
- **Fails closed:** without `APP`, every token is refused unless `ALLOW_ALL_USERS` is set. The end-user IP forwarded to Jivo Auth comes from `REMOTE_ADDR` unless the client's own `NUM_PROXIES` says to read `X-Forwarded-For`.
- **DRF:** `JivoJWTAuthentication` verifies signature (JWKS by `kid`, cached per process in `keys.py`, still usable while Jivo Auth is down), expiry with leeway, issuer and `token_type == "access"`, and answers 403 when `apps` lacks `APP`. It returns a stateless `JivoUser`, or a local user record with `LOCAL_USERS` (`users.get_local_user()`: found by `LOCAL_USER_ID_FIELD`; new rows get the email, or the Jivo ID, as a separate `USERNAME_FIELD`; `LOCAL_USER_LINK_BY_EMAIL` claims the single unlinked row with the token's email).
- **Session login for template sites:** `backends.JivoAuthBackend` checks credentials with Jivo Auth (so Django's `LoginView` and the admin work), `middleware.JivoSessionMiddleware` refreshes the stored tokens and logs the user out when Jivo Auth refuses, and `signals.py` stores tokens on login and ends the remote session on logout. `session.refresh_tokens()` serializes concurrent refreshes through the cache; the service's reuse grace covers refreshes that still race (other processes, lost responses).
- **Tests:** `packages/jivo-auth-client/tests/` (fake auth service patched onto `jivo_auth.http.urlopen`; `tests.Member` is a user model with an `auth_id` column, used with `override_settings(AUTH_USER_MODEL=...)`), plus `JivoAuthClientTests` and `JivoAuthClientLiveTests` in `authentication/tests.py` against this service's real tokens. If you change token claims or the algorithm, update the client package too.

## Known limitations

- Access tokens are stateless. After logout, a session revoke, a password change, deactivation or losing application access, an already-issued access token stays valid until it expires (15 minutes by default), both here and in client apps.
- Only one signing key is published at a time. Rotating `jwt_private.pem` invalidates every outstanding token, so all users must log in again.
- Nginx isn't part of the repo (README Phase 10); `docs/deploy-auth-service.md` has the host config. Static files are served by WhiteNoise from the image.
