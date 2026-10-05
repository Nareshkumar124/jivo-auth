"""
Tests for the staged cleanup migration, disable_local_passwords.py (phase 6).

The migration is staged outside migrations/ until the cleanup release (see
cleanup.md), so no `migrate` runs it early. These tests load that exact file
and run its function against the test database, so the file that will be
applied is the file that was tested. Copy next to the app's other tests and
adapt the lines marked ADAPT.
"""

import importlib.util
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.test import TestCase

User = get_user_model()

# ADAPT: where the migration is staged, and the app's migrations folder.
STAGED = Path(settings.BASE_DIR) / "docs" / "jivo-auth-cleanup" / "0099_disable_local_passwords.py"
MIGRATIONS = Path(settings.BASE_DIR) / "users" / "migrations"


def make_user(username, **kwargs):
    # ADAPT: required fields of the user model.
    return User.objects.create_user(username=username, password="CorrectHorse9!", **kwargs)


def staged_migration():
    spec = importlib.util.spec_from_file_location("staged_disable_local_passwords", STAGED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DisableLocalPasswordsTests(TestCase):

    def setUp(self):
        self.module = staged_migration()
        self.users = [make_user(f"u{i}") for i in range(3)]

    def run_migration(self):
        self.module.disable_local_passwords(apps, None)

    def test_it_is_staged_not_active(self):
        """Delete this test in the cleanup release, when the file moves into migrations/."""
        self.assertFalse(list(MIGRATIONS.glob("*disable_local_passwords*")))

    def test_every_password_becomes_unusable(self):
        self.run_migration()
        for user in User.objects.all():
            self.assertFalse(user.has_usable_password(), user.get_username())
            self.assertFalse(user.check_password("CorrectHorse9!"), user.get_username())

    def test_each_row_gets_its_own_value(self):
        """One shared value would give every user the same session hash."""
        self.run_migration()
        values = list(User.objects.values_list("password", flat=True))
        self.assertEqual(len(values), len(set(values)))

    def test_rows_already_unusable_are_left_alone(self):
        already = make_user("jivo-only")
        User.objects.filter(pk=already.pk).update(password=make_password(None))
        before = User.objects.get(pk=already.pk).password
        self.run_migration()
        self.assertEqual(User.objects.get(pk=already.pk).password, before)

    def test_break_glass_usernames_keep_their_password(self):
        self.module.BREAK_GLASS_USERNAMES = ["u0"]
        self.run_migration()
        self.assertTrue(User.objects.get(**{User.USERNAME_FIELD: "u0"}).check_password("CorrectHorse9!"))
        self.assertFalse(User.objects.get(**{User.USERNAME_FIELD: "u1"}).has_usable_password())

    def test_nothing_but_the_password_changes(self):
        def everything_but_password():
            fields = [f.attname for f in User._meta.concrete_fields if f.name != "password"]
            return list(User.objects.order_by("pk").values(*fields))

        before = everything_but_password()
        self.run_migration()
        self.assertEqual(everything_but_password(), before)
