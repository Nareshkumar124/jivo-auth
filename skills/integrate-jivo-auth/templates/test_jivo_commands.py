"""
Tests for export_jivo_users, link_jivo_users and sync_jivo_users (phase 2).

Copy next to the commands' app as `tests_jivo_commands.py` (or into its
tests package) and adapt the three lines marked ADAPT. The refusals matter
as much as the happy path: an export that leaks hashes into the repository,
or a link that attaches a Jivo ID to the wrong person, is what these guard.

No test calls Jivo Auth: AuthClient is mocked.

Notes from real runs:
- Test settings often use a fast hasher (MD5). Compare an exported hash with
  the stored one, never with an algorithm prefix.
- `create_user` may store a blank email as "" rather than None.
"""

import json
import os
import stat
import tempfile
import uuid
from io import StringIO
from pathlib import Path
from unittest import mock, skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

User = get_user_model()

# ADAPT: the module path of the copied sync command, for the AuthClient mock.
SYNC_MODULE = "<app>.management.commands.sync_jivo_users"
# ADAPT: the model field export maps to first_name (export's FIELDS["first_name"]).
EXPORT_FIRST_NAME_FIELD = "first_name"
LINK_BY_EMAIL = bool(getattr(settings, "JIVO_AUTH", {}).get("LOCAL_USER_LINK_BY_EMAIL"))


def make_user(username, email, **kwargs):
    # ADAPT: required fields of the user model (e.g. name=...).
    kwargs.setdefault(EXPORT_FIRST_NAME_FIELD, username)
    return User.objects.create_user(username=username, password="CorrectHorse9!", email=email, **kwargs)


class _CommandTestCase(TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_command(self, *args):
        """The command's report; the commands write it to stderr."""
        err = StringIO()
        call_command(*args, stdout=StringIO(), stderr=err)
        return err.getvalue()


class ExportJivoUsersTests(_CommandTestCase):

    def export(self, *args):
        path = self.tmp / "users.json"
        report = self.run_command("export_jivo_users", str(path), *args)
        return json.loads(path.read_text()), report, path

    def test_exports_active_unlinked_users(self):
        user = make_user("priya", "Priya.Sharma@jivo.in")
        rows, _, _ = self.export()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_id"], user.pk)
        self.assertEqual(rows[0]["email"], "Priya.Sharma@jivo.in")
        self.assertEqual(rows[0]["password"], user.password)
        self.assertEqual(rows[0]["first_name"], getattr(user, EXPORT_FIRST_NAME_FIELD))

    def test_skips_users_without_email_and_reports_them(self):
        make_user("with-email", "a@jivo.in")
        no_email = make_user("no-email", "")
        rows, report, _ = self.export()
        self.assertEqual([row["email"] for row in rows], ["a@jivo.in"])
        self.assertIn(f"no email: pk={no_email.pk}", report)

    def test_never_sends_staff_flags(self):
        make_user("boss", "boss@jivo.in", is_staff=True, is_superuser=True)
        rows, _, _ = self.export()
        for key in ("is_staff", "is_superuser", "groups", "user_permissions"):
            self.assertNotIn(key, rows[0])

    def test_leaves_out_linked_and_inactive_users(self):
        make_user("linked", "linked@jivo.in", auth_id=uuid.uuid4())
        make_user("gone", "gone@jivo.in", is_active=False)
        make_user("todo", "todo@jivo.in")
        rows, report, _ = self.export()
        self.assertEqual([row["email"] for row in rows], ["todo@jivo.in"])
        self.assertIn("1 inactive (not exported)", report)

    def test_without_password_leaves_out_those_hashes(self):
        keep = make_user("keep", "keep@jivo.in")
        drop = make_user("seeded", "seeded@jivo.in")
        rows, _, _ = self.export("--without-password", str(drop.pk))
        by_pk = {row["source_id"]: row for row in rows}
        self.assertEqual(by_pk[keep.pk]["password"], keep.password)
        self.assertIsNone(by_pk[drop.pk]["password"])

    def test_without_password_refuses_a_key_it_would_miss(self):
        """A typo, or a user who isn't exported, must not let a password through."""
        make_user("keep", "keep@jivo.in")
        linked = make_user("linked", "linked@jivo.in", auth_id=uuid.uuid4())
        path = self.tmp / "users.json"
        with self.assertRaisesMessage(CommandError, str(linked.pk)):
            self.run_command("export_jivo_users", str(path), "--without-password", str(linked.pk))
        self.assertFalse(path.exists())

    def test_file_is_owner_only(self):
        make_user("priya", "priya@jivo.in")
        _, _, path = self.export()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_refuses_to_write_inside_the_project(self):
        target = Path(settings.BASE_DIR) / "users.json"
        with self.assertRaisesMessage(CommandError, "outside the project"):
            self.run_command("export_jivo_users", str(target))
        self.assertFalse(target.exists())

    def test_refuses_to_overwrite(self):
        path = self.tmp / "users.json"
        path.write_text("keep me")
        with self.assertRaisesMessage(CommandError, "exists"):
            self.run_command("export_jivo_users", str(path))
        self.assertEqual(path.read_text(), "keep me")


class LinkJivoUsersTests(_CommandTestCase):

    def mapping(self, *rows, **extra):
        path = self.tmp / f"mapping-{uuid.uuid4().hex}.json"
        path.write_text(json.dumps({"summary": {}, "users": list(rows), **extra}))
        return str(path)

    @staticmethod
    def row(user, auth_id, status="created", email=None):
        return {"source_id": user.pk, "email": (email or user.email).lower(),
                "auth_id": str(auth_id), "status": status, "notes": []}

    def test_sets_auth_id_and_nothing_else(self):
        user = make_user("priya", "priya@jivo.in")
        before = User.objects.filter(pk=user.pk).values().get()
        auth_id = uuid.uuid4()
        self.run_command("link_jivo_users", self.mapping(self.row(user, auth_id)))
        after = User.objects.filter(pk=user.pk).values().get()
        self.assertEqual(after.pop("auth_id"), auth_id)
        before.pop("auth_id")
        self.assertEqual(after, before)

    def test_refuses_when_the_local_email_changed_since_the_export(self):
        user = make_user("priya", "priya@jivo.in")
        report = self.run_command("link_jivo_users", self.mapping(
            self.row(user, uuid.uuid4(), email="someone.else@jivo.in")))
        user.refresh_from_db()
        self.assertIsNone(user.auth_id)
        self.assertIn("local email is now", report)

    def test_refuses_an_auth_id_another_user_already_has(self):
        auth_id = uuid.uuid4()
        holder = make_user("holder", "holder@jivo.in", auth_id=auth_id)
        other = make_user("other", "other@jivo.in")
        report = self.run_command("link_jivo_users", self.mapping(self.row(other, auth_id)))
        other.refresh_from_db()
        self.assertIsNone(other.auth_id)
        self.assertIn(f"auth_id already used by local user pk={holder.pk}", report)

    def test_refuses_to_relink_a_user_to_a_different_id(self):
        original = uuid.uuid4()
        user = make_user("priya", "priya@jivo.in", auth_id=original)
        report = self.run_command("link_jivo_users", self.mapping(self.row(user, uuid.uuid4())))
        user.refresh_from_db()
        self.assertEqual(user.auth_id, original)
        self.assertIn(f"already linked to {original}", report)

    def test_running_again_is_harmless(self):
        user = make_user("priya", "priya@jivo.in")
        path = self.mapping(self.row(user, uuid.uuid4()))
        self.run_command("link_jivo_users", path)
        self.assertIn("0 linked, 1 already linked", self.run_command("link_jivo_users", path))

    def test_dry_run_saves_nothing(self):
        user = make_user("priya", "priya@jivo.in")
        report = self.run_command("link_jivo_users", self.mapping(self.row(user, uuid.uuid4())), "--dry-run")
        user.refresh_from_db()
        self.assertIsNone(user.auth_id)
        self.assertIn("Dry run, nothing saved", report)

    def test_refuses_the_output_of_an_import_dry_run(self):
        user = make_user("priya", "priya@jivo.in")
        with self.assertRaisesMessage(CommandError, "dry run"):
            self.run_command("link_jivo_users", self.mapping(self.row(user, uuid.uuid4()), dry_run=True))


class SyncJivoUsersTests(_CommandTestCase):

    def sync(self, jivo_users, *args):
        with mock.patch(f"{SYNC_MODULE}.AuthClient") as client:
            client.return_value.get_users.return_value = jivo_users
            return self.run_command("sync_jivo_users", *args)

    @staticmethod
    def jivo(user_id, email, first="", last=""):
        return {"id": str(user_id), "email": email, "first_name": first, "last_name": last, "is_active": True}

    def test_refreshes_the_email(self):
        auth_id = uuid.uuid4()
        user = make_user("priya", "Priya@jivo.in", auth_id=auth_id)
        self.sync([self.jivo(auth_id, "priya@jivo.in", "Priya")])
        user.refresh_from_db()
        self.assertEqual(user.email, "priya@jivo.in")

    def test_never_touches_app_fields(self):
        auth_id = uuid.uuid4()
        user = make_user("boss", "boss@jivo.in", auth_id=auth_id, is_staff=True)
        self.sync([{**self.jivo(auth_id, "boss@jivo.in", "Boss"), "is_active": False}])
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)

    @skipUnless(LINK_BY_EMAIL, "decision D4 is off")
    def test_create_missing_links_the_unlinked_row_with_that_email(self):
        """Needs LOCAL_USER_ID_FIELD / LOCAL_USER_LINK_BY_EMAIL set already."""
        user = make_user("priya", "priya@jivo.in")
        auth_id = uuid.uuid4()
        report = self.sync([self.jivo(auth_id, "priya@jivo.in", "Priya")], "--create-missing")
        user.refresh_from_db()
        self.assertEqual(user.auth_id, auth_id)
        self.assertEqual(User.objects.count(), 1)
        self.assertIn("linked by email: priya@jivo.in", report)

    def test_name_field_joins_first_and_last_and_never_blanks(self):
        """For models with one name field (sync's NAME_FIELD)."""
        import importlib
        module = importlib.import_module(SYNC_MODULE)
        auth_id = uuid.uuid4()
        user = make_user("priya", "priya@jivo.in", auth_id=auth_id)
        fields = {**module.FIELDS, "first_name": None, "last_name": None}
        with mock.patch.object(module, "FIELDS", fields), \
                mock.patch.object(module, "NAME_FIELD", EXPORT_FIRST_NAME_FIELD):
            self.sync([self.jivo(auth_id, "priya@jivo.in", "Priya", "Sharma")])
            user.refresh_from_db()
            self.assertEqual(getattr(user, EXPORT_FIRST_NAME_FIELD), "Priya Sharma")
            self.sync([self.jivo(auth_id, "priya@jivo.in")])
            user.refresh_from_db()
            self.assertEqual(getattr(user, EXPORT_FIRST_NAME_FIELD), "Priya Sharma")

    def test_dry_run_saves_nothing(self):
        auth_id = uuid.uuid4()
        user = make_user("priya", "Priya@jivo.in", auth_id=auth_id)
        self.sync([self.jivo(auth_id, "priya@jivo.in")], "--dry-run")
        user.refresh_from_db()
        self.assertEqual(user.email, "Priya@jivo.in")
