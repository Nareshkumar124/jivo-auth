---
name: integrate-jivo-auth
description: Move an existing Django or Django REST Framework application onto the central Jivo Auth service (auth.jivo.in) without losing a user row, foreign key or permission. Collects the prerequisites, analyzes the app's user model and authentication code, adds an auth_id column, imports the existing users into Jivo Auth and links them, then replaces the app's own login, tokens, passwords and user creation with Jivo Auth and verifies the result. Use it whenever a Django/DRF app (API, admin, frontend or mobile backend) should sign in through Jivo Auth, central login, SSO, auth.jivo.in or jivo-auth-client, or its users should be imported or migrated there, even when the request is just "connect <app> to Jivo Auth" or "use Jivo login". Run it once per app.
when_to_use: Any request to integrate, connect, move or migrate a Django or DRF application (or its users, admin or frontend) to Jivo Auth, the central auth/SSO/login service at auth.jivo.in, or to continue an integration that an earlier session started.
argument-hint: "[application-slug]"
---

# Integrate a DRF application with Jivo Auth

Run this inside the repository of the application being integrated (the
"app"). Jivo Auth is the central service at `https://auth.jivo.in` (source:
`https://github.com/Nareshkumar124/jivo-auth`). If an application slug was
given, it is: `$ARGUMENTS`.

## Starting, or picking up where an earlier session stopped

Look for the integration plan first, `docs/jivo-auth-integration.md` in the
app's repository (phase 1 writes it). If it exists, its status line says
which phases are done and what's still open: continue from there, and read
the reference file for the phase you're in. Gates already passed stay
passed; the next one still needs its own go-ahead. `git status` shows work
an earlier session left uncommitted, such as a switched working tree or a
staged cleanup. If there's no plan, start at phase 0.

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
   - If the user volunteers a password in chat anyway, use it only through an environment variable for a smoke test, never write it to a file, and suggest they change it afterwards.
4. **Back up first, then dry-run.** Take a backup of the app's database (`pg_dump -Fc`) before every step that writes user data, and dry-run each data command before running it for real.
5. **Confirm outward actions.** Deploying, running commands on the Jivo Auth host or production, and pushing all need confirmation first.
6. **Never copy staff flags into Jivo Auth.** Don't send `is_staff`, `is_superuser`, groups or permissions to Jivo Auth: its staff can administer every Jivo account. They stay in the app.
7. **Steps are repeatable.** Export, import and link are idempotent, so re-run them rather than patching data by hand.
8. **Stop and ask when the app does something this skill doesn't cover.** Examples: stock `auth.User` with no custom model, multi-tenant users, SSO/OAuth providers, or tokens that other services accept. Describe the situation and the options.
9. **Know what's shared before writing.** Jivo Auth accounts are global, not per environment: importing a test database into production Jivo Auth creates real identities that the production migration later links to. And an app's test or staging database is often shared with another deployment still on the old login. Check both in phase 0 (prerequisites.md, "First") and say what they mean for each later phase. When verifying from shared tables, other deployments write there too: attribute what you see through the app's own logs.
10. **Measure against a baseline.** Many apps already have failing tests. Record them before changing anything (analysis.md, section 7) and report "no new failures" against that list, not "the suite is green".
11. **Report what happened, not what was meant to happen.** Every phase ends with the counts, the checks and anything skipped or failed, as they are. A step that was prepared but not run is reported as prepared.

## Phases

Track these as tasks. Details are in the linked files; read each one when you reach its phase.

- [ ] **0. Prerequisites.** Follow [reference/prerequisites.md](reference/prerequisites.md), starting with which Jivo Auth instance and who else uses the app's database. ⛔ Don't start phase 1 until every item is known or has an owner.
- [ ] **1. Analyze.** Follow [reference/analysis.md](reference/analysis.md): write the integration plan and ⛔ get it approved.
- [ ] **2. Prepare** (additive release, no behaviour change). See below.
- [ ] **3. Migrate users.** Follow [reference/migrate-users.md](reference/migrate-users.md). ⛔ Before the import on Jivo Auth, and ⛔ before linking in the app's production database.
- [ ] **4. Switch authentication.** Follow [reference/switch.md](reference/switch.md) and [reference/frontend.md](reference/frontend.md). ⛔ Before deploying.
- [ ] **5. Verify.** See below.
- [ ] **6. Clean up.** A separate, later release: follow [reference/cleanup.md](reference/cleanup.md). ⛔ It's destructive. If anything still signs in the old way against the same database, prepare it now and apply it later.

### Phase 2: Prepare (ships without changing how anyone signs in)

1. **Install the client package**, `jivo-auth-client` 0.3.0 or later, pinned to the commit from prerequisites item 6. The repository is public, so no credentials are needed.
   - uv: `uv add "jivo-auth-client @ git+https://github.com/Nareshkumar124/jivo-auth.git#subdirectory=packages/jivo-auth-client" --rev <commit>`
   - requirements.txt: `jivo-auth-client @ git+https://github.com/Nareshkumar124/jivo-auth.git@<commit>#subdirectory=packages/jivo-auth-client`
2. **Add `"jivo_auth"` to `INSTALLED_APPS`** and add the settings dict. The package reads `URL`, `APP` and `API_KEY` from the `JIVO_AUTH_URL`, `JIVO_AUTH_APP` and `JIVO_AUTH_API_KEY` environment variables when the dict doesn't set them:
   ```python
   JIVO_AUTH = {
       "LOCAL_USER_ID_FIELD": "auth_id",
       "LOCAL_USER_LINK_BY_EMAIL": True,   # decision D4
   }
   ```
   - **Make sure the variables reach the process.** Many apps don't load `.env` themselves; add `python-dotenv` or set them in the deployment. Apps on `python-decouple` or `django-environ` read `.env` without filling `os.environ`: pass the values explicitly in `JIVO_AUTH` through that loader.
   - **The two `LOCAL_USER_*` keys belong in this release**, not the switch: `sync_jivo_users --create-missing` uses them, and at the package defaults it keys new rows on `username`. They change nothing else until the switch.
   - **Leave `DEFAULT_AUTHENTICATION_CLASSES` unchanged** for now.
3. **Add `auth_id` to the user model:**
   ```python
   auth_id = models.UUIDField(
       null=True, blank=True, unique=True, editable=False,
       help_text="The user's ID in Jivo Auth.",
   )
   ```
   Then run `makemigrations`. The migration adds a nullable column with a unique index and changes no data. On PostgreSQL that's quick; SQLite rebuilds the table, and MySQL may too. Back up before applying it in every environment (migrate-users.md, "Before you start", covers applying it). A dev server running this code fails every user query until the column exists.
4. **Copy the management commands** from [templates/](templates/) into one of the app's own apps (`<app>/management/commands/`):
   - `export_jivo_users.py`
   - `link_jivo_users.py`
   - `sync_jivo_users.py` (optional)

   Adapt only the settings at the top of each: `FIELDS` to the model's field names, and in `sync_jivo_users`, `NAME_FIELD` for a model with one name field instead of first/last.
5. **Add tests** for the commands from [templates/test_jivo_commands.py](templates/test_jivo_commands.py): export skips users without email and never sends staff flags; link refuses email mismatches and IDs that are already used.
6. **Check it:** run the app's test suite and compare it with the baseline, `python manage.py check` (it must report no `jivo_auth.E00x` errors), and `makemigrations --check`.
7. ⛔ Commit and deploy this release. Get the user's confirmation, and follow the app's usual process.

### Phase 5: Verify (right after the switch deploys)

With a test account the user controls (never a real user's password). [templates/jivo_smoke.py](templates/jivo_smoke.py) runs steps 1–2 and the refresh and sign-out checks from the command line, with the credentials in environment variables:

1. **Sign in:** `POST https://auth.jivo.in/api/v1/auth/login/`, then call the app's API with the access token. Expect 200, not 401 or 403.
2. **Mapping:** for a migrated user, `request.user.pk` must be the **same primary key as before the migration**. Check one of their business records through the API.
3. **Admin:** sign in to the app's admin with Jivo credentials as a migrated staff user. A wrong password shows the form again; there's no password field or password page, and nothing creates a user with a password.
4. **Access:** a user without the app's grant gets 403; removing a grant takes effect within 15 minutes.
5. **Refresh in the browser:** keep a signed-in browser tab open past the access token's lifetime (15 minutes). Its requests must go on answering 200 without the user signing in again. The app's access log shows it.
6. **Step-up flows (D9):** confirm against the live sessions API that a fresh confirmation sign-in is accepted once, and an ordinary sign-in is not.
7. **Unmapped users:** list `is_active=True, auth_id=None` users in the app and resolve each with the user.
8. **Checks:** `python manage.py check` is clean, and the test suite has no failures beyond the baseline.

Report the results as they are, including anything that failed or was skipped.

## Ending a session

Whether the run finished or stopped at a gate, leave the next person (or session) able to continue:

- **Update the plan's status line** in `docs/jivo-auth-integration.md`: which phases are done, in which environment, and what's committed, deployed, staged or still uncommitted.
- **List what's open, with an owner:** unmapped users, CORS origins, SMTP, mobile apps still on the old login, the cleanup release and what it waits for.
- **Confirm the export files are gone** (migrate-users.md, section 6), and say where the backups and `mapping.json` are, never their contents.
- **Summarize for the user** in a few lines: what changed, what was verified, what wasn't, and the next gate.

## Templates

All in [templates/](templates/). `scripts/check_templates.py` builds a throwaway Django project around them and runs every template test; run it after changing a template or the client package.

| File | Phase | What it is |
|---|---|---|
| `export_jivo_users.py`, `link_jivo_users.py`, `sync_jivo_users.py` | 2–3 | The migration commands |
| `test_jivo_commands.py` | 2 | Tests for those commands |
| `jivo_test_tokens.py` | 4 | Real RS256 test tokens, verified with a throwaway key |
| `test_jivo_auth.py` | 4 | Tests the switch must pass |
| `fresh_sign_in.py`, `test_fresh_sign_in.py` | 4 | Step-up "type your password again" check (D9), and its tests |
| `break_glass_backend.py` | 4 | Local sign-in for named emergency accounts (D7) |
| `jivo_smoke.py` | 5 | Live check: sign in, call the app, refresh, sign out |
| `disable_local_passwords.py`, `test_jivo_cleanup.py` | 6 | The cleanup data migration, staged until its release, and the tests that run against the staged file |

## Reference

- **Integration guide (humans):** `docs/integrate-drf.md` and `docs/integrate-django.md` in the Jivo Auth repository. The same guide, with the live server's settings, is at `https://auth.jivo.in/admin/docs/` (staff only).
- **API:** `https://auth.jivo.in/api/docs/` (Swagger).
- **Client settings:** `jivo_auth/settings.py` in the package. `python -c "import jivo_auth.settings as s; print(s.__doc__)"` prints every key.
