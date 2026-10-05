# Phase 6: Remove what only served the old authentication

A separate release, after the switch has run cleanly for a while: agree on
how long with the user, e.g. two weeks. It deletes data, so ⛔ get explicit
approval of the exact list, and take a fresh backup first:

```bash
umask 077   # the backup holds every password hash
pg_dump -Fc -f app-before-jivo-cleanup-$(date +%F).dump <db>
```

Before starting, check that:

- `auth_id` is set for every active user who signs in, or each exception is known;
- no code reads the fields or tables below (search again: the analysis may be weeks old);
- **nothing still signs people in the old way against this database.** Shared test and staging databases are often used by another deployment of the app that hasn't switched. Making passwords unusable would lock its users out, and dropping the token tables would break its login outright. Check the database's clients (`pg_stat_activity`) and that no old tokens were issued since the switch (e.g. `SELECT max(created_at) FROM token_blacklist_outstandingtoken`). If something still does, prepare the release (below) and wait;
- the mobile apps sign in with Jivo Auth, if the release removes a 410 stub.

**Prepare now, apply later.** When cleanup can't run yet, write it anyway,
but keep **every** cleanup change out of the code that gets deployed until
the cleanup release. That means the password migration, model fields
removed (a dropped column breaks every query of an old deployment on the
same database), `DeleteModel` migrations, and the `migrate <app> zero`
steps. A migration file in `migrations/` runs on the next `migrate`
anywhere. Either keep the release on an unmerged branch, or stage its files
with a runbook, e.g. in `docs/jivo-auth-cleanup/`, and test the staged
password migration from the suite. Don't run `makemigrations` for cleanup
before then; it would also fail `makemigrations --check` in CI.

## 1. Make local passwords unusable

The `password` column stays, because Django's sessions and admin need it. Its values go. Use a data migration in the user model's app, so every environment gets the same change: [templates/disable_local_passwords.py](../templates/disable_local_passwords.py). Set its app label, model, `USERNAME_FIELD` (e.g. `"email"` for models that sign in by email) and `BREAK_GLASS_USERNAMES` (decision D7). [templates/test_jivo_cleanup.py](../templates/test_jivo_cleanup.py) tests the staged file from the app's suite: every password becomes unusable, each row gets its own value, rows already unusable and break-glass accounts are left alone, nothing else changes, and the file isn't in `migrations/` yet (drop that one test in the cleanup release).

It's irreversible: the old hashes only survive in the backup, which is why the backup comes first.

- **Some admin sessions end.** Django derives each session's check value from the password column, so the sessions of users whose old hash is replaced end, and those staff sign in again with Jivo. Tell them beforehand.
- **One random value per row**, rather than a single shared `update()`, so no two users share a password value or a session hash.

## 2. Drop authentication-only columns

Take the third column of the analysis table (analysis.md, section 2), minus anything the user wants kept. Remove those fields from the model, then run `makemigrations`. The generated `RemoveField` operations are the only schema changes. Read the migration before committing it.

**Never drop:**

- the primary key, `auth_id`, `password` or `last_login`;
- `is_active`, `is_staff`, `is_superuser`, groups or permissions;
- `email`, `first_name` or `last_name`, which are kept as the local copy;
- any business field.

## 3. Drop authentication-only apps and tables

For third-party apps (`rest_framework.authtoken`, `rest_framework_simplejwt.token_blacklist`, knox, djoser, allauth, django-otp, axes, ...):

1. **Drop their tables in each environment** while the app is still installed, which means with the code of the release before the one that removes it: `python manage.py migrate <app_label> zero`, for example `authtoken`, `token_blacklist`, `knox`, `otp_totp`. Make it an explicit step in the deploy notes, since `migrate` alone doesn't do it.
   - **Empty the tables first.** The rows are authentication-only, and some apps' reverse migrations fail on existing data. SimpleJWT's `token_blacklist` stops with `IntegrityError: NOT NULL ... jti` and is left half-unapplied. For example: `python manage.py shell -c "from rest_framework_simplejwt.token_blacklist.models import OutstandingToken; OutstandingToken.objects.all().delete()"`, which cascades to `BlacklistedToken`.
   - **If a reverse migration still fails,** restore the backup rather than repairing `django_migrations` by hand.
2. **Remove the app from `INSTALLED_APPS`**, and remove the package from the dependencies.
3. **Delete its settings, URLs and imports.**

For the app's own authentication models (OTP codes, reset tokens, login attempts): delete the model classes and run `makemigrations`, which generates `DeleteModel`.

## 4. Stale content types and permissions

The removed models leave content types and permissions behind, and they still show up in the admin's permission picker.

- **Remove them:** run `python manage.py remove_stale_contenttypes`. It lists what it will delete and asks first. It also deletes those models' permissions and their user and group assignments.
- **Admin history keeps its entries.** The admin's `LogEntry` rows for those objects stay, but lose their link.

## 5. Afterwards

- **Run the suite:** `python manage.py check`, `makemigrations --check` and the test suite.
- **Deploy** (⛔) and verify: sign-in, the API and the admin, as in phase 5.
- **Update the integration plan** to record what was removed, and when.
- **Leave `auth_id` nullable** unless every row has one. Legacy rows that were never mapped keep their data and simply can't sign in.
