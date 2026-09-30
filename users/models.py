import uuid

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
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

    # Optional, unique when set; stored trimmed and uppercased.
    employee_code = models.CharField(
        max_length=32,
        blank=True,
        default="",
        validators=[
            RegexValidator(
                r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
                "Use letters and digits, optionally with . _ / or -.",
            ),
        ],
        help_text="The person's code in Jivo's HR records, e.g. JIVO1234.",
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
            # Many users may have none; no two may share one.
            models.UniqueConstraint(
                fields=["employee_code"],
                condition=~Q(employee_code=""),
                name="users_user_employee_code_unique",
                violation_error_code="employee_code_taken",
                violation_error_message="Another user already has this employee code.",
            ),
        ]

    def __str__(self):
        return self.email

    @staticmethod
    def normalize_employee_code(code):
        return (code or "").strip().upper()

    def clean_fields(self, exclude=None):
        # Before the format check and the uniqueness check, so " jivo1"
        # passes the first and collides with "JIVO1" in the second.
        self.employee_code = self.normalize_employee_code(self.employee_code)

        super().clean_fields(exclude=exclude)

    def validate_constraints(self, exclude=None):
        # A conditional constraint reports a general error; show a taken
        # employee code on its field instead.
        try:
            super().validate_constraints(exclude=exclude)
        except ValidationError as error:
            errors = error.update_error_dict({})
            general = errors.pop(NON_FIELD_ERRORS, [])

            for item in general:
                key = "employee_code" if item.code == "employee_code_taken" else NON_FIELD_ERRORS
                errors.setdefault(key, []).append(item)

            raise ValidationError(errors) from error

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

        self.employee_code = self.normalize_employee_code(self.employee_code)

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
