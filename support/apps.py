from django.apps import AppConfig


class SupportConfig(AppConfig):
    """Support tickets: departments, teams, tickets and their workflow."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "support"
    verbose_name = "Support"

    def ready(self):
        from . import checks, signals  # noqa: F401  (checks registers itself)

        signals.connect()
