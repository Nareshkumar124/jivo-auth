# Phase 0: Prerequisites

Collect everything below before touching code. Some items you can find or
check yourself. For the rest, ask the user who provides them. Record each
answer, never a secret value, in the integration plan (phase 1).

## Jivo Auth: the service

| # | Item | Value / where it comes from | How to check |
|---|---|---|---|
| 1 | **Auth application URL** | `https://auth.jivo.in` | `curl -s https://auth.jivo.in/api/v1/health/` returns `{"status": "ok", ...}` |
| 2 | **Auth API base URL** | `https://auth.jivo.in/api/v1` (login, refresh, logout, users) | Swagger at `https://auth.jivo.in/api/docs/` |
| 3 | **Signing keys (JWKS)** | `https://auth.jivo.in/.well-known/jwks.json`: RS256, fetched and cached by the client package | `curl -s https://auth.jivo.in/.well-known/jwks.json` lists one key |
| 4 | **Token issuer** (`iss`) | Equals the URL above | Decode any access token |
| 5 | **Jivo Auth version** | Must include `manage.py import_users`, from the commit that ships client 0.3.0 | On the Auth host: `docker compose exec auth python manage.py help import_users` |
| 6 | **Client package commit** | A commit of `Nareshkumar124/jivo-auth` with `jivo-auth-client` ≥ 0.3.0 | `packages/jivo-auth-client/pyproject.toml` at that commit |

## Jivo Auth: access and credentials (ask the user who holds them)

| # | Item | Why it's needed | Notes |
|---|---|---|---|
| 7 | **A Jivo Auth admin account.** Superuser, or the *Application manager* role, at `https://auth.jivo.in/admin/` | Registering the app, creating its API key, granting access, reading the audit log | The **person** uses it. Don't ask for the password, and don't use the account from scripts. |
| 8 | **The application's slug**, e.g. `oms` | It goes in every token's `apps` claim, and the app refuses users without it | Admin → Applications → Add, or on the Auth host: `docker compose exec auth python manage.py app_api_key <slug> --create "<Name>"`. Lowercase, `-`/`_` allowed. Never change it later. |
| 9 | **The application's API key** (`jivo_...`) | Server-side calls: the admin's Jivo sign-in (`JivoAuthBackend`), `sync_jivo_users`, user lookups | Shown once when created. It goes in the app's secret store as `JIVO_AUTH_API_KEY`, never in the repo. Rotating it breaks the old key immediately. |
| 10 | **A way to run `import_users` on Jivo Auth** | Moving the existing users in | Preferred: shell access to the Auth host (`docker compose exec -T auth ...`). Otherwise a Jivo Auth administrator runs it with your file. |
| 11 | **Auth database credentials** | **Not needed, and don't use them.** | Writing to Jivo Auth's database directly skips its validation, email normalization, audit log and access grants. `import_users` is the supported path. If the user offers database credentials, explain this and use item 10 instead. |
| 12 | **CORS for the app's browser frontend** | A browser frontend calls Jivo Auth directly to sign in | The Jivo Auth administrator adds each origin (e.g. `https://oms.jivo.in`) to `CORS_ALLOWED_ORIGINS` in the Auth server's `.env`. Check: `curl -si -X OPTIONS https://auth.jivo.in/api/v1/auth/login/ -H "Origin: https://oms.jivo.in" -H "Access-Control-Request-Method: POST"` must return `access-control-allow-origin`. Not needed for mobile apps or server-rendered pages. |
| 13 | **Test accounts** | Smoke tests after each phase | One account granted the slug and one not granted it, both owned by the user. Their passwords stay with the user, or go in a local env var for a smoke test script, never in the repo. |
| 14 | **Email delivery at Jivo Auth** | Password reset and verification emails | See "Email" on `https://auth.jivo.in/admin/docs/`. Without SMTP, users can't reset passwords themselves and administrators set them in the admin. This affects decisions D1 and D2. |

## The application itself

| # | Item | Why |
|---|---|---|
| 15 | **Environments** (local, staging, production) and how each is deployed | Every phase ships through them in order. Test the migration on staging, or on a copy of production. |
| 16 | **Database access and backups** for each environment | Export, link, and a `pg_dump -Fc` before each data step, with a tested restore |
| 17 | **Where the frontend lives** (this repository, another one, mobile apps) and who changes it | The switch (phase 4) must ship the backend and frontend together |
| 18 | **A maintenance window or cutover time** | Everyone signs in again at the switch, because tokens from the old system stop working |
| 19 | **Environment variables** the app will need | Listed below |

### Environment variables for the app

| Variable | Value | Secret? |
|---|---|---|
| `JIVO_AUTH_URL` | `https://auth.jivo.in` | no |
| `JIVO_AUTH_APP` | the slug (item 8) | no |
| `JIVO_AUTH_API_KEY` | the API key (item 9) | **yes** |
| `JIVO_AUTH_NUM_PROXIES` | reverse proxies in front of the app that add to `X-Forwarded-For` (1 behind Nginx, 0 otherwise). Used for the end-user IP sent with admin sign-ins. | no |

The client package reads every `JIVO_AUTH` key from `JIVO_AUTH_<KEY>` when the settings dict doesn't set it. Add all four to the app's `.env.example` without values.

## Decisions the user must make (record the answers in the plan)

| # | Decision | Options and trade-offs |
|---|---|---|
| D1 | **Keep existing passwords?** | *Yes (recommended):* the export includes the Django password hashes. Jivo Auth keeps any hash it can verify (PBKDF2, Argon2; they're upgraded on first sign-in), so users sign in with the password they already have. *No:* accounts get no password, and every user needs a reset or an admin-set password. That's impractical without SMTP (item 14). Hashes Jivo Auth can't verify (MD5, SHA1, bcrypt) always need a reset. The import reports which. |
| D2 | **Treat the app's emails as verified?** (`--mark-verified`) | Jivo Auth refuses sign-in until an email is verified. Say yes only if the app's addresses are known to belong to their users, e.g. company addresses or ones the app verified. Otherwise every migrated user must verify first, which needs SMTP. |
| D3 | **Link to existing Jivo accounts by email?** | If a Jivo account already exists with a user's email, the import links to it and leaves that account's password and names alone. That's right when the app's emails are trustworthy (see D2). The dry-run lists every link for review. |
| D4 | **Link unmapped rows on first sign-in?** (`LOCAL_USER_LINK_BY_EMAIL`) | Covers users the import skipped or who were added after it. Their first Jivo sign-in claims the one local row with that email and no `auth_id`. Same trust question as D2. Off means such users get a new, empty local row instead. |
| D5 | **Which users to migrate** | Default: active users with an email. Inactive users keep their rows and data, and without an `auth_id` they can't sign in. `--include-inactive` also creates inactive Jivo accounts for them. |
| D6 | **Users without an email, and duplicates** | The import skips them. The user decides per case: add the right email in the app and re-run, or leave the row unmapped. Merging two rows is business-data work and outside this skill. |
| D7 | **A break-glass local superuser?** | A named local account with a local password that can sign in to the admin when Jivo Auth is down. It uses `templates/break_glass_backend.py`, which accepts local passwords only for the usernames listed. Plain `ModelBackend` must not be used, because it would accept every migrated user's old password. Without this, no local password works from the switch on. |
| D8 | **Who creates users from now on?** | Accounts are created in Jivo Auth (admin) and granted the slug. The app gets their local row on first sign-in, or ahead of time with `sync_jivo_users --create-missing`, then assigns app roles. See [switch.md](switch.md#creating-users). |

Stop here if any of items 1–14 or decisions D1–D8 is unknown. Ask the user, and continue once they're settled.
