"""
Make every local password unusable (phase 6, cleanup.md section 1).

Stage this OUTSIDE the app's migrations/ folder (e.g. docs/jivo-auth-cleanup/)
until the cleanup release, and test the staged file from the suite with
test_jivo_cleanup.py. A migration file in migrations/ runs on the next
`migrate` anywhere, and if the database is shared with a deployment that
still signs people in with these passwords, that locks all of them out,
irreversibly. At the cleanup release, copy it into <app_label>/migrations/,
renumber it after the latest migration and set `dependencies`.

Irreversible: the old hashes survive only in the backup taken first. The
column stays; Django's sessions and admin need it. One random unusable value
per row, so no two users share a password value or a session hash. Rows that
are already unusable ("!…") are left alone. Admin sessions of the changed
users end, because Django derives each session's check value from this
column; those staff sign in again with Jivo.
"""

from django.contrib.auth.hashers import make_password
from django.db import migrations

APP_LABEL, MODEL = "<app_label>", "<UserModel>"
USERNAME_FIELD = "username"   # historical models don't carry USERNAME_FIELD
BREAK_GLASS_USERNAMES = []    # decision D7: the same names as in settings, if any


def disable_local_passwords(apps, schema_editor):
    User = apps.get_model(APP_LABEL, MODEL)
    users = (
        User.objects
        .exclude(**{f"{USERNAME_FIELD}__in": BREAK_GLASS_USERNAMES})
        .exclude(password__startswith="!")
    )
    batch = []
    for user in users.only("pk").iterator():
        user.password = make_password(None)   # a different random unusable value per row
        batch.append(user)
        if len(batch) == 1000:
            User.objects.bulk_update(batch, ["password"])
            batch = []
    User.objects.bulk_update(batch, ["password"])


class Migration(migrations.Migration):
    dependencies = [(APP_LABEL, "<latest migration at the cleanup release>")]
    operations = [migrations.RunPython(disable_local_passwords, migrations.RunPython.noop)]
