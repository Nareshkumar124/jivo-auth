import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class UserSession(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sessions",
    )

    refresh_jti = models.CharField(
        max_length=255,
        unique=True,
    )

    # The token the current one replaced, accepted again for
    # settings.REFRESH_TOKEN_REUSE_GRACE after rotated_at.
    previous_refresh_jti = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
    )

    rotated_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    device_name = models.CharField(
        max_length=255,
        blank=True,
    )

    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
    )

    user_agent = models.TextField(
        blank=True,
    )

    created_at = models.DateTimeField(
        default=timezone.now,
    )

    last_used_at = models.DateTimeField(
        default=timezone.now,
    )

    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-last_used_at"]

    @property
    def is_active(self):
        return self.revoked_at is None

    def __str__(self):
        return f"{self.user.email} - {self.device_name}"



