"""
Import an existing application's users, so they can sign in with Jivo Auth
and the application can map its own user records to Jivo user IDs.

    python manage.py import_users oms users.json > mapping.json
    docker compose exec -T auth python manage.py import_users oms - < users.json > mapping.json

The input is a JSON list, one object per user of the application:

    {"source_id": 42,                  required: the application's own user ID, echoed back
     "email": "alice@jivo.in",         required
     "first_name": "Alice", "last_name": "Smith",
     "password": "pbkdf2_sha256$...",  optional: the application's Django password hash
     "is_active": true,
     "employee_code": "JIVO1234"}

The output (stdout) maps every source_id to a Jivo user ID, or says why it
was skipped. It's safe to run again: users imported before are "linked".
"""

import json
import sys

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import UNUSABLE_PASSWORD_PREFIX, identify_hasher, make_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models.functions import Lower

from applications.models import Application


FIELDS = {"source_id", "email", "first_name", "last_name", "password", "is_active", "employee_code"}


def normalize_email(value):
    if not isinstance(value, str):
        return None

    email = get_user_model().objects.normalize_email(value.strip()).lower()

    try:
        validate_email(email)
    except ValidationError:
        return None

    return email


def password_hash(value):
    """(hash to store, note): the application's hash if Jivo Auth can verify it."""

    if not value or not isinstance(value, str) or value.startswith(UNUSABLE_PASSWORD_PREFIX):
        return make_password(None), "no usable password: set one in the admin or send a reset"

    try:
        hasher = identify_hasher(value)
        hasher.decode(value)
    except (ValueError, IndexError, TypeError):
        algorithm = value.split("$", 1)[0] if "$" in value else "unknown"
        return make_password(None), (
            f"password hash ({algorithm}) can't be verified here: set one in the admin or send a reset"
        )

    return value, f"password kept ({hasher.algorithm})"


def messages(error):
    return "; ".join(
        f"{field}: {' '.join(errors)}" if field != "__all__" else " ".join(errors)
        for field, errors in error.message_dict.items()
    )


class Command(BaseCommand):
    help = (
        "Import an application's existing users (JSON) and grant them the "
        "application. Prints a JSON mapping of each source_id to a Jivo user ID."
    )

    def add_arguments(self, parser):
        parser.add_argument("slug", help="The application's slug; imported users get access to it.")
        parser.add_argument("file", help="The users JSON file, or - for stdin.")
        parser.add_argument(
            "--mark-verified",
            action="store_true",
            help=(
                "Mark new accounts' emails verified. Only when the application's "
                "addresses are known to belong to their users: unverified accounts "
                "can't sign in while REQUIRE_VERIFIED_EMAIL is on."
            ),
        )
        parser.add_argument("--dry-run", action="store_true", help="Report what would happen, change nothing.")

    def handle(self, *args, slug, file, mark_verified, dry_run, **options):

        application = Application.objects.filter(slug=slug).first()

        if application is None:
            raise CommandError(f"No application with slug {slug!r}.")

        if not application.is_active:
            raise CommandError(f"Application {slug!r} is inactive.")

        rows = self.read(file)

        with transaction.atomic():
            results = self.import_rows(rows, application, mark_verified)

            if dry_run:
                transaction.set_rollback(True)

        summary = {status: 0 for status in ("created", "linked", "skipped")}

        for result in results:
            summary[result["status"]] += 1

        # Not json.dump(): OutputWrapper ends every write() with a newline.
        self.stdout.write(
            json.dumps(
                {
                    "application": slug,
                    "dry_run": dry_run,
                    "summary": summary,
                    "users": results,
                },
                indent=2,
            )
        )

        prefix = "Dry run, nothing saved: " if dry_run else ""
        self.stderr.write(
            f"{prefix}{summary['created']} created, {summary['linked']} linked "
            f"to existing accounts, {summary['skipped']} skipped."
        )

    def read(self, file):
        try:
            if file == "-":
                data = json.load(sys.stdin)
            else:
                with open(file, encoding="utf-8") as handle:
                    data = json.load(handle)
        except (OSError, ValueError) as exc:
            raise CommandError(f"Can't read {file}: {exc}") from exc

        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            raise CommandError("The input must be a JSON list of objects.")

        seen = set()

        for number, row in enumerate(data, start=1):
            source_id = row.get("source_id")

            if source_id is None or source_id == "":
                raise CommandError(f"Entry {number} has no source_id.")

            if str(source_id) in seen:
                raise CommandError(f"source_id {source_id!r} appears more than once.")

            seen.add(str(source_id))

            unknown = set(row) - FIELDS

            if unknown:
                raise CommandError(f"Entry {number} has unknown fields: {', '.join(sorted(unknown))}.")

        return data

    def import_rows(self, rows, application, mark_verified):
        User = get_user_model()

        emails = [normalize_email(row.get("email")) for row in rows]
        counts = {}

        for email in emails:
            if email:
                counts[email] = counts.get(email, 0) + 1

        existing = {
            user.email.lower(): user
            for user in User.objects.annotate(email_lower=Lower("email")).filter(email_lower__in=list(counts))
        }

        results = []
        granted = []

        for row, email in zip(rows, emails):
            result = {"source_id": row["source_id"], "email": email or row.get("email"), "auth_id": None}
            results.append(result)

            if not email:
                result.update(status="skipped", notes=["no valid email address"])
                continue

            if counts[email] > 1:
                result.update(status="skipped", notes=["another source user has the same email"])
                continue

            user = existing.get(email)

            if user is not None:
                result.update(status="linked", auth_id=str(user.id), notes=self.account_notes(user))
                granted.append(user)
                continue

            user, notes = self.create_user(row, email, mark_verified)

            if user is None:
                result.update(status="skipped", notes=notes)
                continue

            result.update(status="created", auth_id=str(user.id), notes=notes)
            granted.append(user)

        # One add() for everyone, so the audit log records each new grant.
        application.users.add(*granted)

        return results

    def account_notes(self, user):
        notes = ["an account with this email already exists; its password, names and status are unchanged"]

        if not user.is_active:
            notes.append("that account is deactivated")

        if not user.is_verified:
            notes.append("that account's email isn't verified, so it can't sign in yet")

        return notes

    def create_user(self, row, email, mark_verified):
        User = get_user_model()

        is_active = row.get("is_active", True)
        name_length = User._meta.get_field("first_name").max_length
        user = User(
            email=email,
            first_name=str(row.get("first_name") or "").strip()[:name_length],
            last_name=str(row.get("last_name") or "").strip()[:name_length],
            employee_code=str(row.get("employee_code") or ""),
            is_active=is_active if isinstance(is_active, bool) else True,
            is_verified=mark_verified,
        )

        user.password, password_note = password_hash(row.get("password"))
        notes = [password_note]

        try:
            user.full_clean(exclude=["password"])
        except ValidationError as error:
            if "employee_code" not in error.message_dict:
                return None, [messages(error)]

            notes.append(f"employee code not imported: {' '.join(error.message_dict['employee_code'])}")
            user.employee_code = ""

            try:
                user.full_clean(exclude=["password"])
            except ValidationError as error:
                return None, [messages(error)]

        if not mark_verified:
            notes.append("email not verified: the user can't sign in until it is")

        user._audit_source = "import"
        user.save()

        return user, notes
