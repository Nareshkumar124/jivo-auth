from django.db import migrations, models


class Migration(migrations.Migration):
    """The permission's placeholder model; it never has a table."""

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Dashboard",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ],
            options={
                "verbose_name": "JI dashboard",
                "verbose_name_plural": "JI dashboards",
                "permissions": [("view_dashboards", "Can see the JI dashboards")],
                "managed": False,
                "default_permissions": (),
            },
        ),
    ]
