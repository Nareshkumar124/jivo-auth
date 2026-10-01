from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditEvent(models.Model):
    """
    Something security-relevant that happened to an account: sign-ins,
    password changes, access changes. Written by audit.services.record().
    """

    class Type(models.TextChoices):
        LOGIN_SUCCEEDED = "login_succeeded", "Signed in"
        LOGIN_FAILED = "login_failed", "Sign-in failed"
        LOGOUT = "logout", "Signed out"
        TOKEN_REUSED = "token_reused", "Refresh token reused"
        SESSIONS_REVOKED = "sessions_revoked", "Sessions revoked"
        PASSWORD_CHANGED = "password_changed", "Password changed"
        PASSWORD_RESET_REQUESTED = "password_reset_requested", "Password reset requested"
        PASSWORD_RESET = "password_reset", "Password reset"
        ACCOUNT_CREATED = "account_created", "Account created"
        ACCOUNT_DEACTIVATED = "account_deactivated", "Account deactivated"
        ACCOUNT_REACTIVATED = "account_reactivated", "Account reactivated"
        EMAIL_VERIFIED = "email_verified", "Email verified"
        ACCESS_GRANTED = "access_granted", "Application access granted"
        ACCESS_REVOKED = "access_revoked", "Application access removed"
        API_KEY_ROTATED = "api_key_rotated", "API key rotated"

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    SEVERITIES = {
        Type.LOGIN_FAILED: Severity.WARNING,
        Type.ACCOUNT_DEACTIVATED: Severity.WARNING,
        Type.API_KEY_ROTATED: Severity.WARNING,
        Type.TOKEN_REUSED: Severity.CRITICAL,
    }

    created_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )

    type = models.CharField(
        max_length=40,
        choices=Type.choices,
    )

    # The account the event is about.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_events",
    )

    # Who caused it, when that's someone else (an administrator).
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    # The account's email at the time, or the address tried at sign-in; kept
    # when the user is deleted.
    email = models.CharField(
        max_length=254,
        blank=True,
    )

    application = models.ForeignKey(
        "applications.Application",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_events",
    )

    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
    )

    user_agent = models.CharField(
        max_length=255,
        blank=True,
    )

    details = models.JSONField(
        default=dict,
        blank=True,
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["type", "created_at"]),
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["ip_address", "created_at"]),
        ]

    def __str__(self):
        return f"{self.get_type_display()}: {self.email or '-'}"

    @property
    def severity(self):
        return self.SEVERITIES.get(self.type, self.Severity.INFO)

    REASONS = {
        "invalid_credentials": "wrong email or password",
        "email_not_verified": "email not verified",
        "no_active_account": "account inactive",
    }

    SOURCES = {
        "registration": "self-registration",
        "admin": "by an administrator",
        "system": "from the command line or a script",
        "import": "imported from an application's user list",
    }

    @property
    def summary(self):
        """One line of details for lists, e.g. "wrong email or password"."""

        details = self.details or {}

        if "reason" in details:
            return self.REASONS.get(details["reason"], details["reason"])

        if "source" in details:
            return self.SOURCES.get(details["source"], details["source"])

        if self.type == self.Type.SESSIONS_REVOKED:
            count = details.get("count", 0)
            scope = " (all devices)" if details.get("scope") == "all" else ""
            return f"{count} session{'s' if count != 1 else ''}{scope}"

        if "sessions_revoked" in details:
            count = details["sessions_revoked"]
            return f"{count} session{'s' if count != 1 else ''} ended"

        if details.get("key_prefix"):
            return f"new key {details['key_prefix']}…"

        if details.get("device"):
            return details["device"]

        return ""

    @classmethod
    def types_with_severity(cls, severity):
        if severity == cls.Severity.INFO:
            return [
                value
                for value in cls.Type.values
                if value not in cls.SEVERITIES
            ]

        return [
            value
            for value, level in cls.SEVERITIES.items()
            if level == severity
        ]
