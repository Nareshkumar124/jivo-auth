#!/usr/bin/env python3
"""
Test every template against a throwaway Django project.

    python skills/integrate-jivo-auth/scripts/check_templates.py [--keep DIR] [-v]

It builds a small project the way the skill tells an integrator to (a custom
user model with `auth_id`, the commands copied in, the switch settings, a
"me" endpoint, a 410 login stub, a password-free admin, the break-glass
backend, the staged cleanup migration), fills in the templates' ADAPT lines,
and runs the template tests in it. Nothing calls Jivo Auth.

Run it after changing a template or the client package. It needs Django,
djangorestframework, jivo-auth-client, PyJWT and cryptography importable;
from the Jivo Auth repository, `uv run python …` has them all.

--keep DIR writes the project to DIR (left in place) instead of a temp dir,
to look at a failure. Exit status is the test run's.
"""

import argparse
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

# Placeholders in the templates, and what the sample project calls them.
ADAPT = {
    "<slug>": "sample",
    "<app>": "users",
    "<app_label>": "users",
    "<UserModel>": "User",
    "<latest migration at the cleanup release>": "0001_initial",
}

# template -> where it goes in the project (relative to the project root).
PLACEMENT = {
    "export_jivo_users.py": "users/management/commands/export_jivo_users.py",
    "link_jivo_users.py": "users/management/commands/link_jivo_users.py",
    "sync_jivo_users.py": "users/management/commands/sync_jivo_users.py",
    "jivo_test_tokens.py": "users/jivo_test_tokens.py",
    "fresh_sign_in.py": "users/fresh_sign_in.py",
    "break_glass_backend.py": "users/backends.py",
    "disable_local_passwords.py": "docs/jivo-auth-cleanup/0099_disable_local_passwords.py",
    "test_jivo_commands.py": "users/test_jivo_commands.py",
    "test_jivo_auth.py": "users/test_jivo_auth.py",
    "test_fresh_sign_in.py": "users/test_fresh_sign_in.py",
    "test_jivo_cleanup.py": "users/test_jivo_cleanup.py",
}

SETTINGS = '''
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "sample"   # a throwaway project; plain `check` doesn't mind a short key
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "jivo_auth",
    "users",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "jivo_auth.middleware.JivoSessionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "project.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "users.User"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]   # fast, like most test settings
STATIC_URL = "/static/"
USE_TZ = True

# The switch (switch.md): Jivo tokens for the API, Jivo email + password for the admin.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["jivo_auth.authentication.JivoJWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}
JIVO_AUTH = {
    "URL": "https://auth.jivo.in",
    "APP": "sample",
    "API_KEY": "sample-key-never-used-nothing-here-calls-jivo-auth",
    "LOCAL_USERS": True,
    "LOCAL_USER_ID_FIELD": "auth_id",
    "LOCAL_USER_LINK_BY_EMAIL": True,
}
AUTHENTICATION_BACKENDS = [
    "jivo_auth.backends.JivoAuthBackend",
    "users.backends.BreakGlassBackend",   # decision D7
]
BREAK_GLASS_USERNAMES = ["breakglass"]
'''

URLS = '''
from django.contrib import admin
from django.urls import path
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class MeView(APIView):
    def get(self, request):
        return Response({"id": request.user.pk, "email": request.user.email})


class LoginGoneView(APIView):
    """The old sign-in URL, kept for clients that can't update on the same day."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, *args, **kwargs):
        return Response({"detail": "Sign in at Jivo Auth.",
                         "sign_in_url": "https://auth.jivo.in/api/v1/auth/login/"}, status=410)

    get = post


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/me/", MeView.as_view()),
    path("api/auth/login/", LoginGoneView.as_view()),
]
'''

MODELS = '''
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    auth_id = models.UUIDField(
        null=True, blank=True, unique=True, editable=False,
        help_text="The user's ID in Jivo Auth.",
    )
'''

ADMIN = '''
from django.contrib import admin

from .models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    """Based on ModelAdmin, not auth's UserAdmin: no password form, no <id>/password/ page."""
    list_display = ("username", "email", "auth_id", "is_active", "is_staff")
    fields = ("username", "email", "first_name", "last_name", "auth_id",
              "is_active", "is_staff", "is_superuser", "groups", "user_permissions")
    readonly_fields = ("auth_id", "email", "first_name", "last_name")   # Jivo-owned

    def has_add_permission(self, request):
        return False   # accounts are created in Jivo Auth
'''

MANAGE = '''
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
'''


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip("\n"), encoding="utf-8")


def build(root):
    write(root, "manage.py", MANAGE)
    write(root, "project/__init__.py", "")
    write(root, "project/settings.py", SETTINGS)
    write(root, "project/urls.py", URLS)
    write(root, "users/__init__.py", "")
    write(root, "users/models.py", MODELS)
    write(root, "users/admin.py", ADMIN)
    write(root, "users/migrations/__init__.py", "")
    write(root, "users/management/__init__.py", "")
    write(root, "users/management/commands/__init__.py", "")

    for name, destination in PLACEMENT.items():
        text = (TEMPLATES / name).read_text(encoding="utf-8")

        for placeholder, value in ADAPT.items():
            text = text.replace(placeholder, value)

        write(root, destination, text)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--keep", metavar="DIR", help="Build the project here and leave it in place.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show every test name.")
    args = parser.parse_args()

    try:
        import cryptography  # noqa: F401
        import django  # noqa: F401
        import jivo_auth  # noqa: F401
        import jwt  # noqa: F401
        import rest_framework  # noqa: F401
    except ImportError as exc:
        print(f"Missing dependency: {exc}. Run this with an environment that has Django, DRF, "
              "jivo-auth-client, PyJWT and cryptography (e.g. `uv run python …` in the Jivo Auth repo).")
        return 2

    missing = sorted(set(PLACEMENT) - {path.name for path in TEMPLATES.iterdir()})

    if missing:
        print(f"Templates missing: {', '.join(missing)}")
        return 2

    # The live smoke test can't run here; at least make sure it parses.
    py_compile.compile(str(TEMPLATES / "jivo_smoke.py"), doraise=True)

    if args.keep:
        root = Path(args.keep).resolve()
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
    else:
        temporary = tempfile.TemporaryDirectory(prefix="jivo-auth-templates-")
        root = Path(temporary.name)

    build(root)
    print(f"Sample project: {root}")

    def manage(*command):
        return subprocess.run([sys.executable, "manage.py", *command], cwd=root).returncode

    for command in (("makemigrations", "users", "--noinput"), ("check",)):
        if manage(*command):
            return 1

    return manage("test", "users", "-v", "2" if args.verbose else "1")


if __name__ == "__main__":
    sys.exit(main())
