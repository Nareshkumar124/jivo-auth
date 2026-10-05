# Phase 0: Prerequisites

Collect everything below before touching code. Some items you can find or
check yourself. For the rest, ask the user who provides them. Record each
answer, never a secret value, in the integration plan (phase 1).

## First: which Jivo Auth, and which app database

Settle these two before anything else, because they decide how risky every
later step is.

- **Which Jivo Auth instance.** Usually production `https://auth.jivo.in`. A
  local or staging checkout of `jivo-auth` is also possible. If one is chosen,
  use its URL everywhere this skill writes `auth.jivo.in`: `JIVO_AUTH_URL`,
  the CORS check below, the 410 stub's `sign_in_url`, the frontend's auth
  base URL, and `jivo_smoke.py`'s `JIVO_AUTH_URL`.
  **Jivo Auth accounts are global, not per environment.** Importing a test
  database into production creates real, verified sign-in identities from
  test data, with the test database's passwords. When the app's production
  database is migrated later, its users are *linked* to those accounts (D3)
  and keep the test passwords. When the app side is a test or staging
  database and the target is production, give the user the options:
  - a local or staging Jivo Auth instead;
  - import only a few test accounts;
  - import without the test passwords (D1 = no, or `--without-password`);
  - accept it, and plan resets for those accounts before the production
    migration.

  Record the choice, and say it again at the import gate.
- **Who else uses the app's database.** Test and staging databases are often
  shared with another deployment of the app, still on the old login. Check
  for other clients (PostgreSQL):
  ```sql
  SELECT client_addr, usename, application_name, count(*) FROM pg_stat_activity
  WHERE datname = current_database() GROUP BY 1, 2, 3;
  ```
  Your own connections show up too; `client_addr` is empty for local socket
  connections, and other roles' rows need `pg_read_all_stats`. Also check
  whether old tokens are still being issued, e.g. the newest row of
  `token_blacklist_outstandingtoken` or `authtoken_token`.

  What the other deployment sees:
  - **Phase 2:** an extra nullable `auth_id` column, which is harmless to old
    code because Django selects columns by name.
  - **Phases 3–5:** `auth_id` values, then this deployment's writes to shared
    rows. Emails become lowercase at each user's first Jivo sign-in, rows are
    linked or created on sign-in (D4), and `sync_jivo_users` rewrites names
    and emails. An old deployment that matches emails case-sensitively can
    trip over that.
  - **Phase 6:** it decides this one. Cleanup on a shared database locks that
    deployment's users out.

  Record it in the plan.

## Jivo Auth: the service

| # | Item | Value / where it comes from | How to check |
|---|---|---|---|
| 1 | **Auth application URL** | `https://auth.jivo.in` | `curl -s https://auth.jivo.in/api/v1/health/` returns `{"status": "ok", ...}` |
| 2 | **Auth API base URL** | `https://auth.jivo.in/api/v1` (login, refresh, logout, users) | Swagger at `https://auth.jivo.in/api/docs/` |
| 3 | **Signing keys (JWKS)** | `https://auth.jivo.in/.well-known/jwks.json`: RS256, fetched and cached by the client package | `curl -s https://auth.jivo.in/.well-known/jwks.json` lists one key |
| 4 | **Token issuer** (`iss`) | Equals the URL above | Decode any access token |
| 5 | **Jivo Auth version** | Must include `manage.py import_users`, from the commit that ships client 0.3.0 | On the Auth host: `docker compose exec auth python manage.py help import_users`. (The import's dry run confirms it too.) |
| 6 | **Client package commit** | A commit of `Nareshkumar124/jivo-auth` with `jivo-auth-client` ≥ 0.3.0 | `packages/jivo-auth-client/pyproject.toml` at that commit |

## Jivo Auth: access and credentials (ask the user who holds them)

| # | Item | Why it's needed | Notes |
|---|---|---|---|
| 7 | **A Jivo Auth admin account.** Superuser, or the *Application manager* role, at `https://auth.jivo.in/admin/` | Registering the app, creating its API key, granting access, reading the audit log | The **person** uses it. Don't ask for the password, and don't use the account from scripts. |
| 8 | **The application's slug**, e.g. `oms` | It goes in every token's `apps` claim, and the app refuses users without it | Admin → Applications → Add, or on the Auth host: `docker compose exec auth python manage.py app_api_key <slug> --create "<Name>"`. Lowercase, `-`/`_` allowed. Never change it later. |
| 9 | **The application's API key** (`jivo_...`) | Server-side calls: the admin's Jivo sign-in (`JivoAuthBackend`), `sync_jivo_users`, user lookups | Shown once when created. The user puts it in the app's `.env` or secret store as `JIVO_AUTH_API_KEY` themselves, never in the repo or chat. Rotating it breaks the old key immediately. Check it from the app, which also proves the settings wiring, printing a count only: `python manage.py shell -c "from jivo_auth.client import AuthClient; print(len(AuthClient().get_users()))"`. The underlying call is `GET {URL}/api/v1/apps/users/` with the `X-Jivo-App-Key` header. The key can only list users; it can't create them. |
| 10 | **A way to run `import_users` on Jivo Auth** | Moving the existing users in | Preferred: SSH to the Auth host, running the command in the existing container with the file on **stdin**: `ssh <host> "cd <dir> && docker compose exec -T auth python manage.py import_users <slug> - --dry-run" < users.json > mapping-dry.json`. The export, which holds password hashes, is never written on the host, so there's nothing to clean up there. Otherwise a Jivo Auth administrator runs it with your file. |
| 11 | **Auth database credentials** | **Not needed, and don't use them.** | Writing to Jivo Auth's database directly skips its validation, email normalization, audit log and access grants. `import_users` is the supported path. If the user offers database credentials, explain this and use item 10 instead. |
| 12 | **CORS for the app's browser frontend** | A browser frontend calls Jivo Auth directly to sign in | The Jivo Auth administrator adds each origin to `CORS_ALLOWED_ORIGINS` in the Auth server's `.env`: the production one (e.g. `https://oms.jivo.in`) and any dev origin such as `http://localhost:5173`. The service reads `.env` when its container is created, so after editing, the Auth host's owner **recreates** it (⛔: it briefly interrupts sign-in for every Jivo app): `docker compose up -d --no-build --force-recreate auth`. A `docker compose restart` keeps the old value. Check: `curl -si -X OPTIONS https://auth.jivo.in/api/v1/auth/login/ -H "Origin: <origin>" -H "Access-Control-Request-Method: POST"`. The preflight answers 200 **even when the origin is refused**; only an `access-control-allow-origin` header in the response means allowed. Note the `access-control-allow-headers` list too (accept, authorization, content-type, user-agent, x-csrftoken, x-requested-with): the frontend must not send its own `X-*` headers to Jivo Auth (frontend.md). Not needed for mobile apps or server-rendered pages. |
| 13 | **Test accounts** | Smoke tests after each phase | One account granted the slug and one not granted it, both owned by the user. Their passwords stay with the user, or go in a local env var for `templates/jivo_smoke.py`, never in the repo. |
| 14 | **Email delivery at Jivo Auth** | Password reset and verification emails | See "Email" on `https://auth.jivo.in/admin/docs/`. On the Auth host, check both without printing the rest of `.env`: `grep -E '^EMAIL_(BACKEND|HOST)=' .env`. `smtp` with no host means email is effectively off. Without SMTP, users can't reset passwords themselves, administrators set them in the admin, and the frontend shows no "Forgot password?" link. This affects decisions D1 and D2. |

## The application itself

| # | Item | Why |
|---|---|---|
| 15 | **Environments** (local, staging, production) and how each is deployed | Every phase ships through them in order. Test the migration on staging, or on a copy of production. |
| 16 | **Database access and backups** for each environment | Export, link, and a `pg_dump -Fc` before each data step, with a tested restore. Whether the database is shared with another deployment (see "First" above). |
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
| D1 | **Keep existing passwords?** | *Yes (recommended):* the export includes the Django password hashes. Jivo Auth keeps any hash it can verify (PBKDF2, Argon2; they're upgraded on first sign-in), so users sign in with the password they already have. *No:* accounts get no password, and every user needs a reset or an admin-set password. That's impractical without SMTP (item 14). Hashes Jivo Auth can't verify (MD5, SHA1, bcrypt) always need a reset. The import reports which. **Either way, look for accounts still on a password committed in the repository.** Seed and "create superuser" commands often have a default (`--password` with a default value). Check those accounts with `django.contrib.auth.hashers.check_password(<that default>, user.password)`, printing only yes or no. Don't use `user.check_password()`: it re-saves the hash when the hasher's settings changed, which is a write. Export them with `--without-password <pk,...>`, so the published default never becomes a Jivo credential; an administrator sets their password in Jivo Auth. |
| D2 | **Treat the app's emails as verified?** (`--mark-verified`) | Jivo Auth refuses sign-in until an email is verified. Say yes only if the app's addresses are known to belong to their users, e.g. company addresses or ones the app verified. Otherwise every migrated user must verify first, which needs SMTP. Show the user the email domains among the users in scope (`GROUP BY lower(split_part(email, '@', 2))`). Personal mail and placeholder domains (`example.com`, made-up internal ones) are exactly what "verified" would vouch for. |
| D3 | **Link to existing Jivo accounts by email?** | If a Jivo account already exists with a user's email, the import links to it and leaves that account's password and names alone. That's right when the app's emails are trustworthy (see D2). The dry-run lists every link for review. Accounts an earlier *test* import created are linked like any other and keep that test database's passwords: flag them for a reset. |
| D4 | **Link unmapped rows on first sign-in?** (`LOCAL_USER_LINK_BY_EMAIL`) | Covers users the import skipped or who were added after it. Their first Jivo sign-in claims the one local row with that email and no `auth_id`. Same trust question as D2. Off means such users get a new, empty local row instead. |
| D5 | **Which users to migrate** | Default: active users with an email. Inactive users keep their rows and data, and without an `auth_id` they can't sign in. `--include-inactive` also creates inactive Jivo accounts for them. |
| D6 | **Users without an email, and duplicates** | The import skips them. The user decides per case: add the right email in the app and re-run, or leave the row unmapped. Merging two rows is business-data work and outside this skill. |
| D7 | **A break-glass local superuser?** | A named local account with a local password that can sign in to the admin when Jivo Auth is down. It uses `templates/break_glass_backend.py`, which accepts local passwords only for the usernames listed. Plain `ModelBackend` must not be used, because it would accept every migrated user's old password. Without this, no local password works from the switch on. |
| D8 | **Who creates users from now on?** | Accounts are created in Jivo Auth (admin) and granted the slug. The app gets their local row on first sign-in, or ahead of time with `sync_jivo_users --create-missing`, then assigns app roles. See [switch.md](switch.md#creating-users). The application API key can only *list* users, so the app's "create user" screens become "pick an existing Jivo user". |
| D9 | **Password re-confirmation ("type your password again before X")** | Only if the analysis finds such a flow (`check_password` outside sign-in). Options: *Fresh Jivo sign-in (recommended):* the client signs in to Jivo Auth again and the app checks the resulting session (`templates/fresh_sign_in.py`), so the password never reaches the app. *Forward the password:* the app's server calls Jivo Auth login with it, which is simpler but keeps the app handling passwords. *Drop the step.* |

Stop here if the Jivo Auth instance, any of items 1–14 or decisions D1–D9 is unknown. Ask the user, and continue once they're settled. Many of these are quick multiple-choice questions; asking several at once saves the user round trips.
