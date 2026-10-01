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
- **Fields that are NOT NULL with no default.** The client creates local rows for new Jivo users with only an ID, an email, an unusable password and (when there is one) a username. Other required fields make that fail, so each one needs a default, `null=True`, or a value from `sync_jivo_users`.
- **Row counts:** total, active, without email, with duplicate emails.

**Stock `auth.User`.** You can't add a column to it. Stop and give the user two options:

- **(a) Swap to a custom user model on the same table.** Create `accounts.User(AbstractUser)` with `class Meta: db_table = "auth_user"`, fake its initial migration, and repoint FKs and content types. This is the procedure from Django ticket #25313. It's risky and needs a rehearsal on a copy of production, but it gives a real `auth_id` column.
- **(b) Keep stock `User`** and store the Jivo ID in `username` (`LOCAL_USER_ID_FIELD="username"`, the package default). There's no schema change, but no separate `auth_id` column either.

Continue only once the user has chosen.

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
- **Tests:** how they authenticate. `force_authenticate`/`force_login` with a local user keep working; tests that sign in through the old endpoints must change.
- **Anything else** that issues or accepts the app's own tokens: other services, mobile apps, cron jobs or scripts calling the API with a stored token. Each one needs its own plan, for example a Jivo service account or a server-side API key.

## 7. Write the integration plan

Save it in the app's repository, e.g. `docs/jivo-auth-integration.md`, so the team and later sessions can follow it. Include:

1. **Prerequisites and decisions D1–D8** with their answers. No secret values.
2. **The user model**, its field classification table (section 2), and row counts.
3. **The authentication-only models and tables** to remove at cleanup.
4. **User relationships**, all preserved.
5. **Change list by file:** what's removed, rewritten, kept and added.
6. **Release plan:** prepare → migrate → switch → verify → cleanup, with the environment order, the rehearsal on staging or a production copy, the cutover time, and who deploys the frontend.
7. **Rollback for each release** (see [switch.md](switch.md#rollback)).
8. **Risks and open questions.**

⛔ Show the plan to the user and wait for approval before phase 2.
