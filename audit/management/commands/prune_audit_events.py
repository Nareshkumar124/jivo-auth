from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from audit.models import AuditEvent


class Command(BaseCommand):
    help = (
        "Delete audit events older than AUDIT_LOG_RETENTION_DAYS (or "
        "--older-than-days). Run it daily, e.g. from cron."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than-days",
            type=int,
            default=settings.AUDIT_LOG_RETENTION_DAYS,
            help="Keep events newer than this many days.",
        )

    def handle(self, *args, older_than_days, **options):
        cutoff = timezone.now() - timedelta(days=older_than_days)

        deleted, _ = AuditEvent.objects.filter(created_at__lt=cutoff).delete()

        self.stdout.write(
            f"Deleted {deleted} audit event(s) older than {older_than_days} days."
        )
