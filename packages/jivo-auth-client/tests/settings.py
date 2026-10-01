"""A minimal consuming application, using Django's stock User model."""

from pathlib import Path


SECRET_KEY = "jivo-auth-client-tests"

DEBUG = False

ALLOWED_HOSTS = ["testserver"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "jivo_auth",
    # Member, a user model with an auth_id column.
    "tests",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "jivo_auth.middleware.JivoSessionMiddleware",
]

ROOT_URLCONF = "tests.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [Path(__file__).parent / "templates"],
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
}

AUTHENTICATION_BACKENDS = [
    "jivo_auth.backends.JivoAuthBackend",
    # Local-only accounts, e.g. an emergency superuser.
    "django.contrib.auth.backends.ModelBackend",
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "jivo_auth.authentication.JivoJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

JIVO_AUTH = {
    "URL": "https://auth.example.test",
    "APP": "oms",
    "API_KEY": "jivo_test-api-key",
}

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/whoami/"
LOGOUT_REDIRECT_URL = "/login/"

USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
