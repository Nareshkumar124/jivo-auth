from django.core.management.base import BaseCommand, CommandError

from applications.models import Application


class Command(BaseCommand):
    help = (
        "Create a new API key for an application and print it. Replaces the "
        "application's current key, which stops working immediately."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "slug",
            help="The application's slug.",
        )

        parser.add_argument(
            "--create",
            metavar="NAME",
            help="Create the application with this name if it doesn't exist.",
        )

    def handle(self, *args, slug, create, **options):

        application = Application.objects.filter(slug=slug).first()

        if application is None:
            if not create:
                raise CommandError(
                    f"No application with slug {slug!r}. Pass --create NAME "
                    "to create it."
                )

            application = Application(slug=slug, name=create)
            application.full_clean()
            application.save()

            self.stderr.write(f"Created application {application}.")

        raw_key = application.set_new_api_key()

        self.stderr.write(
            "New API key (shown only once; store it as the application's "
            "JIVO_AUTH['API_KEY']):"
        )
        self.stdout.write(raw_key)
