# Phase 1: Analyze the application

Read the code; don't guess. Every item below ends up in the integration plan
with file paths. Use the searches as a starting point, then read what they find.

The searches use `git grep`, which only looks at tracked files, so virtualenvs,
`node_modules` and build output stay out of the results. Outside a git
repository, use `grep -rn` with `--exclude-dir=.venv --exclude-dir=venv
--exclude-dir=node_modules --exclude-dir=.git`.

## 1. The user model

```bash
git grep -n -E "AUTH_USER_MODEL" -- '*.py'
git grep -n -E "AbstractUser|AbstractBaseUser|PermissionsMixin" -- '*.py'
```

Record:

- **The model and its base.** One of `AbstractUser`, `AbstractBaseUser` (+ `PermissionsMixin`), or **stock `django.contrib.auth.models.User`** (no `AUTH_USER_MODEL`).
- **Its primary key type.** `int`/`bigint` or UUID. It stays as it is either way.
- **`USERNAME_FIELD`, `EMAIL_FIELD`, `REQUIRED_FIELDS`.** Also whether email is unique, nullable or blank, and whether emails are unique case-insensitively in the data:
  ```sql
  SELECT lower(email), count(*) FROM <table> GROUP BY 1 HAVING count(*) > 1;
  ```
- **Fields that are NOT NULL with no default.** The client creates local rows for new Jivo users with only an ID, an email, an unusable password and (when there is one) a username. Other required fields make that fail, so each one needs a default, `null=True`, or a value from `sync_jivo_users`. A NOT NULL `CharField` is fine: Django fills in `""`.
- **Names.** Some models drop `first_name`/`last_name` (`first_name = None`) for one `name` field. Map export's `first_name` to it and `last_name` to `None`, and set `NAME_FIELD` in `sync_jivo_users`, which joins Jivo's first and last name back into it. Jivo Auth's names are at most 100 characters, and `import_users` truncates longer ones silently. A longer local name then comes back shortened through `sync_jivo_users`, so check `max(length(name))`.
- **Row counts:** total, active, without email, with duplicate emails, staff and superusers, and the hash algorithms in use (`split_part(password, '$', 1)`).

**Stock `auth.User`.** You can't add a column to it. Stop and give the user two options:

- **(a) Swap to a custom user model on the same table.** Create `accounts.User(AbstractUser)` with `class Meta: db_table = "auth_user"`, fake its initial migration, and repoint FKs and content types. This is the procedure from Django ticket #25313. It's risky and needs a rehearsal on a copy of production, but it gives a real `auth_id` column.
- **(b) Keep stock `User`** and store the Jivo ID in `username` (`LOCAL_USER_ID_FIELD="username"`, the package default). There's no schema change, but no separate `auth_id` column either.

Continue only once the user has chosen.

### How settings read the environment

The client package reads `JIVO_AUTH_<KEY>` with `os.getenv`. `python-decouple` and `django-environ` read `.env` without putting anything into `os.environ`, so a value that lives only in `.env` never reaches the package. In that case, pass `URL`, `APP`, `API_KEY` and `NUM_PROXIES` explicitly in the `JIVO_AUTH` dict through the app's own loader, e.g. `config("JIVO_AUTH_API_KEY", default="")`.

## 2. Classify every field on the user model

Put each field in one of three columns. This table is the core of the plan.

| Moves to Jivo Auth (the app keeps a read-only copy or drops it) | Stays in the app, unchanged | Authentication-only: removed in cleanup |
|---|---|---|
| `email`, `first_name`, `last_name`, and an employee code if there is one. Keep the columns: they're a local copy refreshed from Jivo, used by FKs' display and search. | The primary key, `auth_id`, `is_active` (now "blocked in this app"), `is_staff`, `is_superuser`, `groups`, `user_permissions`, roles, departments, phone (if it's contact data), preferences, every business field, `date_joined`, `last_login` | `email_verified`/`is_verified`, OTP secrets, MFA devices, `failed_login_attempts`, `locked_until`, `password_changed_at`, reset/verification codes, stored refresh tokens, "must change password" |

**Special cases:**

- **`password` stays as a column.** `AbstractBaseUser` needs it: Django derives session hashes from it, and the admin and `check_password` use it. Cleanup makes every value unusable instead of dropping the column.
- **`username`** stays. It's no longer a credential. New local users get their email as the username, or the Jivo ID if that's taken.
- **A field that is both** (e.g. a phone used for OTP sign-in and for contact): it stays. Ask the user.

## 3. Authentication-related models and tables

```bash
git grep -n -E "authtoken|rest_framework_simplejwt|token_blacklist|knox|djoser|dj_rest_auth|allauth|oauth2_provider|django_otp|axes|two_factor" -- '*.py' '*.txt' '*.toml' '*.cfg'
git grep -n -E "class .*Token|class .*OTP|class .*Otp|class .*LoginAttempt|class .*PasswordReset|class .*Verification" -- '*models*.py'
```

List each one: whether it's an installed app or the app's own model, its tables, and what points at it. All of it is authentication-only. It stays until cleanup, and nothing business-related may reference it. If something does, flag it.

## 4. Relationships to users (all preserved)

```bash
git grep -n -E "AUTH_USER_MODEL|get_user_model\(\)|ForeignKey\(User|OneToOneField\(User|ManyToManyField\(User|\"auth.User\"|'auth.User'" -- '*.py'
```

List every FK, O2O and M2M to the user model, with `on_delete`. They're kept as they are: the rows they point at don't change. This list is the evidence for "no business data is lost". Also note any **generic relations** or **stored user IDs** in JSON or text columns, which must keep pointing at local primary keys.

## 5. Authentication and authorization logic

```bash
git grep -n -E "DEFAULT_AUTHENTICATION_CLASSES|DEFAULT_PERMISSION_CLASSES|AUTHENTICATION_BACKENDS|SIMPLE_JWT|REST_KNOX|DJOSER|REST_AUTH|LOGIN_URL|SESSION_" -- '*.py'
git grep -n -E "authenticate\(|login\(|logout\(|set_password|check_password|make_password|TokenObtainPairView|TokenRefreshView|obtain_auth_token|ObtainAuthToken|RefreshToken|AccessToken" -- '*.py'
git grep -n -E "BasePermission|has_permission|has_object_permission|permission_classes|IsAdminUser|DjangoModelPermissions|user_passes_test|permission_required" -- '*.py'
git grep -n -E "post_save.*User|user_logged_in|user_logged_out|pre_save.*User" -- '*.py'
```

Also search for things the patterns above miss:

```bash
git grep -n -E "JWTAuthentication\(\)|TokenAuthentication\(\)|\.authenticate\(request\)" -- '*.py'   # middleware decoding tokens itself (audit logging, request logging)
git grep -n -E "set_password\(|check_password\(" -- '*.py' ':!*test*'   # every place that writes or checks a local password
git grep -n -E "auth/login|/login/" -- 'Dockerfile*' '*compose*.y*ml' '*.yaml' '*.yml' '*.sh' '*.bat'   # healthchecks and scripts hitting the old login
git grep -n -E "add_argument\(.*--password|default=.*[Pp]assword" -- '*.py'   # seed commands with a committed default password (D1)
```

- **`check_password` outside sign-in** is usually a step-up confirmation ("type your password again before X"). It needs decision D9.
- **`set_password` outside the users app** is a second user-management screen, e.g. a module's own admin page that creates its users. It changes just like the main one.
- **A healthcheck that requests the old login URL** (expecting 405 or 200) fails once that URL answers 410 or 404. Point it at a health endpoint.

For each hit, record which of these it is:

- **Remove:** sign-in, sign-out, token issue/refresh/verify, registration, password reset/change, email verification, OTP.
- **Rewrite:** user creation, anything that sets or checks passwords, code that reads removed fields.
- **Keep:** authorization, meaning permissions, roles and object ownership. These are app logic and keep working, because `request.user` is the same local row.

Note custom authentication classes, middleware, throttles keyed on the user, and signals such as "create a Token on user save".

## 6. APIs, serializers, admin, UI and tests that touch users

```bash
git grep -n -E "class .*User.*Serializer|password|confirm_password|new_password" -- '*.py'
git grep -n -E "admin.site.register\(User|@admin.register\(User|UserAdmin|UserCreationForm|UserChangeForm" -- '*.py'
git grep -n -E "force_authenticate|force_login|client.login\(|create_user\(|create_superuser\(" -- '*.py'
```

- **Endpoints:** list, detail, "me", create, update and delete for users, and which fields they accept.
  - **Flag any authorization field that users can edit about themselves,** such as a "me" or profile endpoint that accepts `role`, `is_staff`, `groups` or a department that drives permissions. It's an existing hole, and the switch keeps it. Report it in the plan and ask whether to fix it in this work.
- **Admin:** the user admin's forms, fieldsets and actions, and custom admin views.
- **Frontend:** sign-in, sign-up, forgot and reset password, change password, profile edit, user-management screens (especially "create user with password"), and where tokens are stored and refreshed. Search the frontend code too (`login`, `token`, `refresh`, `Authorization`, `localStorage`).
- **Tests:** how they authenticate. `force_authenticate`/`force_login` with a local user keep working; tests that sign in through the old endpoints must change. Also look for tests that pin the old login response, rate-limit the login, validate passwords on user creation, or keep an allowlist of public routes; they change at the switch.
- **Anything else** that issues or accepts the app's own tokens: other services, mobile apps, cron jobs or scripts calling the API with a stored token. Each one needs its own plan, for example a Jivo service account or a server-side API key.

## 7. Baseline the test suite

Run the app's suite once now, with its own test settings, and keep the list
of failing tests. Many apps already fail some tests, e.g. tests that need a
PostgreSQL-only feature under SQLite, or fixtures nobody maintains. "No new
failures" is then the measure for every later phase.

Record the commit in the plan, and compare later on a pristine copy of it
rather than by reverting the working tree, which matters if a dev server is
reloading from it:

```bash
BASE=<commit recorded in the plan>
mkdir -p /tmp/app-baseline && git archive "$BASE" | tar -x -C /tmp/app-baseline   # copy .env too if settings need it
(cd /tmp/app-baseline && python manage.py test <test settings> > ../baseline.log 2>&1)
python manage.py test <test settings> > now.log 2>&1
grep -E '^(FAIL|ERROR):' /tmp/baseline.log | LC_ALL=C sort -u > before.txt
grep -E '^(FAIL|ERROR):' now.log | LC_ALL=C sort -u > after.txt
LC_ALL=C comm -13 before.txt after.txt   # new failures: should be empty
```

If the test settings use a real database server, Django creates
`test_<db name>` there. On a shared server that name may belong to someone
else's run: never confirm deleting an existing test database, and don't pass
`--noinput`.

## 8. Write the integration plan

Save it in the app's repository, e.g. `docs/jivo-auth-integration.md`, so the team and later sessions can follow it. Include:

1. **Prerequisites and decisions D1–D9** with their answers, including which Jivo Auth instance and who else uses the database. No secret values.
2. **The user model**, its field classification table (section 2), and row counts.
3. **The authentication-only models and tables** to remove at cleanup.
4. **User relationships**, all preserved.
5. **Change list by file:** what's removed, rewritten, kept and added.
6. **The test baseline** (section 7): how many tests, and which already fail.
7. **Release plan:** prepare → migrate → switch → verify → cleanup, with the environment order, the rehearsal on staging or a production copy, the cutover time, and who deploys the frontend.
8. **Rollback for each release** (see [switch.md](switch.md#rollback)).
9. **Risks and open questions.**

⛔ Show the plan to the user and wait for approval before phase 2.
