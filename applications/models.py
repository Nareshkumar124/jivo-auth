import hashlib
import secrets
import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone


API_KEY_PREFIX = "jivo_"


def hash_api_key(raw_key):
    # API keys are long random strings, so a fast hash is enough and lets
    # the key be looked up by its hash.
    return hashlib.sha256(raw_key.encode()).hexdigest()


class Application(models.Model):
    """
    A Jivo application that trusts this service's tokens. Users get access
    per application, and tokens list the slugs of the applications a user
    may use in their `apps` claim.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    slug = models.CharField(
        max_length=50,
        unique=True,
        validators=[
            RegexValidator(
                r"^[a-z0-9][a-z0-9_-]*$",
                "Use lowercase letters, digits, hyphens and underscores.",
            ),
        ],
        help_text=(
            "Identifier in tokens' `apps` claim, e.g. `oms`. The "
            "application sets the same value as JIVO_AUTH['APP']."
        ),
    )

    name = models.CharField(
        max_length=100,
    )

    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Inactive applications are left out of new tokens and their "
            "API key stops working."
        ),
    )

    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="applications",
        help_text="Users who may use this application.",
    )

    api_key_hash = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        editable=False,
    )

    api_key_prefix = models.CharField(
        max_length=16,
        blank=True,
        editable=False,
        help_text="First characters of the current API key, to identify it.",
    )

    api_key_created_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
    )

    created_at = models.DateTimeField(
        default=timezone.now,
    )

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return f"{self.name} ({self.slug})"

    def set_new_api_key(self):
        """
        Replace the API key and return the new raw key. Only its hash is
        stored, so the raw key can't be shown again.
        """

        raw_key = API_KEY_PREFIX + secrets.token_urlsafe(32)

        self.api_key_hash = hash_api_key(raw_key)
        self.api_key_prefix = raw_key[:12]
        self.api_key_created_at = timezone.now()

        self.save(
            update_fields=[
                "api_key_hash",
                "api_key_prefix",
                "api_key_created_at",
            ]
        )

        from audit.models import AuditEvent
        from audit.services import record

        record(
            AuditEvent.Type.API_KEY_ROTATED,
            application=self,
            key_prefix=self.api_key_prefix,
        )

        return raw_key

    @classmethod
    def from_api_key(cls, raw_key):
        if not raw_key:
            return None

        return cls.objects.filter(
            api_key_hash=hash_api_key(raw_key),
            is_active=True,
        ).first()


def app_slugs(user):
    """Slugs of the active applications the user may use."""

    return list(
        Application.objects
        .filter(users=user, is_active=True)
        .order_by("slug")
        .values_list("slug", flat=True)
    )
