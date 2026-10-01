---
name: integrate-jivo-auth
description: Move an existing Django REST Framework application onto the central Jivo Auth service (auth.jivo.in). Collects the prerequisites, analyzes the app's user model and authentication code, adds an auth_id column, imports the existing users into Jivo Auth and maps them, then replaces the app's own login, tokens, passwords and user creation with Jivo Auth. Every app-specific user record, relationship and permission stays in the app.
when_to_use: Use when asked to integrate, connect or migrate a DRF or Django application (API, admin, frontend) to Jivo Auth, the central auth/SSO/login service, or to "use auth.jivo.in" for its users. Works for any number of apps; run it once per app.
argument-hint: "[application-slug]"
---

# Integrate a DRF application with Jivo Auth

Run this inside the repository of the application being integrated (the
"app"). Jivo Auth is the central service at `https://auth.jivo.in` (source:
`https://github.com/Nareshkumar124/jivo-auth`). If an application slug was
given, it is: `$ARGUMENTS`.

## The end state

| Owned by Jivo Auth | Owned by the app, unchanged |
|---|---|
| Email, password, first and last name, employee code | The app's user table: **same rows, same primary keys, same foreign keys** |
| Email verification, "active" for every Jivo app | App roles, groups, permissions, `is_staff`, `is_superuser` |
| Sign-in, tokens, sessions, password reset | Local `is_active` (blocks the user in this app only) |
| Which users may use the app (per-app grants) | Every business and profile field |

- **One new column.** The app's user model gets `auth_id`, a unique UUID that holds the user's Jivo ID.
- **Requests.** Clients send `Authorization: Bearer <Jivo access token>`. `jivo_auth.authentication.JivoJWTAuthentication` verifies it locally, with no call to Jivo Auth per request. With `LOCAL_USERS` and `LOCAL_USER_ID_FIELD="auth_id"` it returns **the same local user row as before**, so views, querysets, permissions and FK writes keep working.
- **Admin sign-in.** Staff sign in to the app's admin with their Jivo email and password (`JivoAuthBackend`).

## Rules for the whole run

1. **Never lose business data.** Don't delete, merge or rewrite user rows or anything that points at them. The only schema changes are adding `auth_id`, and, in the last phase, dropping columns and tables that only served the old authentication.
2. **Gates (⛔) need the user's explicit go-ahead.** Approval at one gate doesn't carry over to the next. Before a gate, show what will happen and what it changes.
3. **Keep secrets out of the repo and the chat.**
   - The API key and any credentials go in environment variables (the app's `.env` or secret store). Never put them in code, commits or logs.
   - Never ask for passwords in chat. The person holding a Jivo Auth admin account does the admin steps themselves.
   - The user export contains password hashes. Write it outside the repository with mode 600, and delete it once the import is verified.
4. **Back up first, then dry-run.** Take a backup of the app's database (`pg_dump -Fc`) before every step that writes user data, and dry-run each data command before running it for real.
5. **Confirm outward actions.** Deploying, running commands on the Jivo Auth host or production, and pushing all need confirmation first.
6. **Never copy staff flags into Jivo Auth.** Don't send `is_staff`, `is_superuser`, groups or permissions to Jivo Auth: its staff can administer every Jivo account. They stay in the app.
7. **Steps are repeatable.** Export, import and link are idempotent, so re-run them rather than patching data by hand.
8. **Stop and ask when the app does something this skill doesn't cover.** Examples: stock `auth.User` with no custom model, multi-tenant users, SSO/OAuth providers, or tokens that other services accept. Describe the situation and the options.

## Phases

Track these as tasks. Details are in the linked files; read each one when you reach its phase.

- [ ] **0. Prerequisites.** Follow [reference/prerequisites.md](reference/prerequisites.md). ⛔ Don't start phase 1 until every item is known or has an owner.
- [ ] **1. Analyze.** Follow [reference/analysis.md](reference/analysis.md): write the integration plan and ⛔ get it approved.
- [ ] **2. Prepare** (additive release, no behaviour change). See below.
- [ ] **3. Migrate users.** Follow [reference/migrate-users.md](reference/migrate-users.md). ⛔ Before the import on Jivo Auth, and ⛔ before linking in the app's production database.
- [ ] **4. Switch authentication.** Follow [reference/switch.md](reference/switch.md) and [reference/frontend.md](reference/frontend.md). ⛔ Before deploying.
- [ ] **5. Verify.** See below.
- [ ] **6. Clean up.** A separate, later release: follow [reference/cleanup.md](reference/cleanup.md). ⛔ It's destructive.

### Phase 2: Prepare (ships without changing how anyone signs in)

1. **Install the client package**, `jivo-auth-client` 0.3.0 or later, pinned to a commit. The repository is public, so no credentials are needed.
   - uv: `uv add "jivo-auth-client @ git+https://github.com/Nareshkumar124/jivo-auth.git#subdirectory=packages/jivo-auth-client" --rev <commit>`
   - requirements.txt: `jivo-auth-client @ git+https://github.com/Nareshkumar124/jivo-auth.git@<commit>#subdirectory=packages/jivo-auth-client`
2. **Add `"jivo_auth"` to `INSTALLED_APPS`** and add `JIVO_AUTH = {}`. The package reads `URL`, `APP` and `API_KEY` from the `JIVO_AUTH_URL`, `JIVO_AUTH_APP` and `JIVO_AUTH_API_KEY` environment variables.
   - **Make sure the variables reach the process.** Many apps don't load `.env` themselves; add `python-dotenv` or set them in the deployment.
   - **Leave `DEFAULT_AUTHENTICATION_CLASSES` unchanged** for now.
3. **Add `auth_id` to the user model:**
   ```python
   auth_id = models.UUIDField(
       null=True, blank=True, unique=True, editable=False,
       help_text="The user's ID in Jivo Auth.",
   )
   ```
   Then run `makemigrations`. The migration adds a nullable column with a unique index and changes no data. On PostgreSQL that's quick; SQLite rebuilds the table, and MySQL may too. Back up before applying it in every environment.
4. **Copy the management commands** from [templates/](templates/) into one of the app's own apps (`<app>/management/commands/`):
   - `export_jivo_users.py`
   - `link_jivo_users.py`
   - `sync_jivo_users.py` (optional)

   Adapt only the `FIELDS` mapping at the top of each to the model's field names.
5. **Add tests** for the commands: export skips users without email; link refuses email mismatches and IDs that are already used.
6. **Check it:** run the app's test suite, `python manage.py check` (it must report no `jivo_auth.E00x` errors), and `makemigrations --check`.
7. ⛔ Commit and deploy this release. Get the user's confirmation, and follow the app's usual process.

### Phase 5: Verify (right after the switch deploys)

With a test account the user controls (never a real user's password):

1. **Sign in:** `POST https://auth.jivo.in/api/v1/auth/login/`, then call the app's API with the access token. Expect 200, not 401 or 403.
2. **Mapping:** for a migrated user, `request.user.pk` must be the **same primary key as before the migration**. Check one of their business records through the API.
3. **Admin:** sign in to the app's admin with Jivo credentials as a migrated staff user.
4. **Access:** a user without the app's grant gets 403; removing a grant takes effect within 15 minutes.
5. **Unmapped users:** list `is_active=True, auth_id=None` users in the app and resolve each with the user.
6. **Checks:** `python manage.py check` is clean, and the test suite passes.

Report the results as they are, including anything that failed or was skipped.

## Reference

- **Integration guide (humans):** `docs/integrate-drf.md` and `docs/integrate-django.md` in the Jivo Auth repository. The same guide, with the live server's settings, is at `https://auth.jivo.in/admin/docs/` (staff only).
- **API:** `https://auth.jivo.in/api/docs/` (Swagger).
- **Client settings:** `jivo_auth/settings.py` in the package. `python -c "import jivo_auth.settings as s; print(s.__doc__)"` prints every key.
