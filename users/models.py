import uuid

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class UserManager(BaseUserManager):

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")

        # Emails are case-insensitive identifiers; store them lowercased.
        email = self.normalize_email(email).lower()

        user = self.model(
            email=email,
            **extra_fields,
        )

        if password:
            user.set_password(password)

        user.save(using=self._db)

        return user

    def get_by_natural_key(self, email):
        # Used by authenticate(), so login works whatever case is typed.
        return self.get(email__iexact=email)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        # Created from the command line by someone who knows the address.
        extra_fields.setdefault("is_verified", True)

        if not extra_fields.get("is_staff"):
            raise ValueError("Superuser must have is_staff=True")

        if not extra_fields.get("is_superuser"):
            raise ValueError("Superuser must have is_superuser=True")

        return self.create_user(
            email=email,
            password=password,
            **extra_fields,
        )


class User(AbstractBaseUser, PermissionsMixin):

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    email = models.EmailField(
        unique=True,
        db_index=True,
    )

    first_name = models.CharField(
        max_length=100,
        blank=True,
    )

    last_name = models.CharField(
        max_length=100,
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
    )

    is_verified = models.BooleanField(
        default=False,
    )

    is_staff = models.BooleanField(
        default=False,
    )

    created_at = models.DateTimeField(
        default=timezone.now,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    objects = UserManager()

    USERNAME_FIELD = "email"

    REQUIRED_FIELDS = []

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                name="users_user_email_ci_unique",
            ),
        ]

    def __str__(self):
        return self.email

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def get_short_name(self):
        return self.first_name or self.email

    @classmethod
    def from_db(cls, db, field_names, values):
        user = super().from_db(db, field_names, values)

        # To notice activation changes on save.
        user._saved_is_active = user.__dict__.get("is_active")

        return user

    def set_unusable_password(self):
        super().set_unusable_password()

        self._password_disabled = True

    def save(self, *args, **kwargs):
        # set_password() leaves the raw password in _password until save;
        # check_password() clears it when it only upgrades the hash, so a
        # login with an old hash doesn't count as a change.
        password_changed = not self._state.adding and (
            self._password is not None
            or getattr(self, "_password_disabled", False)
        )

        saved_is_active = getattr(self, "_saved_is_active", None)
        status_changed = (
            not self._state.adding
            and saved_is_active is not None
            and saved_is_active != self.is_active
        )

        super().save(*args, **kwargs)

        self._password_disabled = False
        self._saved_is_active = self.is_active

        from .services import (
            account_status_changed,
            end_sessions_after_password_change,
        )

        # Whoever changed it (API, admin, `manage.py changepassword`), the
        # old sessions and reset links must stop working.
        if password_changed:
            end_sessions_after_password_change(self)

        if status_changed:
            account_status_changed(self)


class PasswordResetToken(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="password_reset_tokens",
    )

    token_hash = models.CharField(
        max_length=128,
        unique=True,
    )

    expires_at = models.DateTimeField()

    used_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        default=timezone.now,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return str(self.user_id) # type: ignore

class EmailVerificationToken(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="email_verification_tokens",
    )

    token_hash = models.CharField(
        max_length=128,
        unique=True,
    )

    expires_at = models.DateTimeField()

    used_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        default=timezone.now,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return str(self.user_id) # type: ignore
