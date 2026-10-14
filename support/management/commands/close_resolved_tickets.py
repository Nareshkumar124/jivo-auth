from django.core.management.base import BaseCommand

from support.models import SupportSettings
from support.services import close_stale_resolved


class Command(BaseCommand):
    help = (
        "Close resolved tickets the user hasn't answered within the number of "
        "days set in Support settings (auto_close_after_days). Run it daily."
    )

    def handle(self, *args, **options):
        config = SupportSettings.load()

        if not config.auto_close_after_days:
            self.stdout.write("Automatic closing is off (auto_close_after_days = 0).")
            return

        closed = close_stale_resolved(config=config)

        self.stdout.write(f"Closed {closed} ticket(s) resolved more than {config.auto_close_after_days} days ago.")
