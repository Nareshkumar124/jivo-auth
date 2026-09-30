from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    # The DatabaseCache table from settings.CACHES (throttle counters), so
    # `migrate` is all a deployment needs. No-op if it already exists.
    call_command(
        "createcachetable",
        database=schema_editor.connection.alias,
        verbosity=0,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("applications", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            create_cache_table,
            migrations.RunPython.noop,
        ),
    ]
