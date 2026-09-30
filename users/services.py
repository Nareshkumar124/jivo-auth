"""
Password and email-verification flows, shared by the API views and the
password pages this service hosts.
"""

import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from authentication.services import revoke_all_sessions

from .models import EmailVerificationToken, PasswordResetToken


logger = logging.getLogger(__name__)

PASSWORD_RESET_TOKEN_LIFETIME = timedelta(minutes=15)
EMAIL_VERIFICATION_TOKEN_LIFETIME = timedelta(hours=24)


class InvalidTokenError(Exception):
    """A reset or verification token is unknown, used or expired."""


def hash_token(raw_token):
    return hashlib.sha256(
        raw_token.encode()
    ).hexdigest()


def token_link(template, raw_token):
    # Tokens are URL-safe, so they need no quoting.
    return template.replace("{token}", raw_token)


def _send(subject, message, user):
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=None,
            recipient_list=[user.email],
        )

    # A mail outage must not turn into a 500 (which, for password resets,
    # would reveal that the account exists).
    except Exception:
        logger.exception(
            "Failed to send %r email to user %s",
            subject,
            user.pk,
        )


def _create_token(model, user, lifetime):
    raw_token = secrets.token_urlsafe(48)

    model.objects.create(
        user=user,
        token_hash=hash_token(raw_token),
        expires_at=timezone.now() + lifetime,
    )

    return raw_token


def _use_token(model, raw_token, name):
    """
    Lock and return the unused token of an active user, or raise
    InvalidTokenError. Must run inside a transaction.
    """

    token = (
        model.objects
        .select_for_update(of=("self",))
        .select_related("user")
        .filter(
            token_hash=hash_token(raw_token),
            used_at__isnull=True,
            user__is_active=True,
        )
        .first()
    )

    if not token:
        raise InvalidTokenError(f"Invalid {name} token.")

    if token.expires_at <= timezone.now():
        raise InvalidTokenError(f"{name.capitalize()} token has expired.")

    return token


def end_sessions_after_password_change(user):
    """
    Log the user out everywhere: every refresh token is revoked and any
    pending reset tokens stop working. User.save() calls this whenever the
    password changes.
    """

    PasswordResetToken.objects.filter(
        user=user,
        used_at__isnull=True,
    ).update(
        used_at=timezone.now()
    )

    revoke_all_sessions(user)


def set_new_password(user, password):
    """Save the new password; saving logs the user out everywhere."""

    user.set_password(password)

    user.save(
        update_fields=[
            "password",
            "updated_at",
        ]
    )


def send_password_reset(user):
    """Email the user a reset link. Returns the raw token."""

    raw_token = _create_token(
        PasswordResetToken,
        user,
        PASSWORD_RESET_TOKEN_LIFETIME,
    )

    _send(
        "Reset your Jivo password",
        (
            "Use this link to reset your Jivo password. "
            "It expires in 15 minutes.\n\n"
            f"{token_link(settings.PASSWORD_RESET_URL, raw_token)}\n\n"
            "If you didn't request a password reset, "
            "you can ignore this email."
        ),
        user,
    )

    return raw_token


def reset_password(raw_token, new_password):
    """Set a new password with a reset token, or raise InvalidTokenError."""

    with transaction.atomic():

        # Locked so two concurrent requests can't both use the token.
        reset_token = _use_token(
            PasswordResetToken,
            raw_token,
            "reset",
        )

        set_new_password(
            reset_token.user,
            new_password,
        )


def send_email_verification(user):
    """
    Email the user a verification link; earlier links stop working.
    Returns the raw token.
    """

    EmailVerificationToken.objects.filter(
        user=user,
        used_at__isnull=True,
    ).update(
        used_at=timezone.now()
    )

    raw_token = _create_token(
        EmailVerificationToken,
        user,
        EMAIL_VERIFICATION_TOKEN_LIFETIME,
    )

    _send(
        "Verify your Jivo email address",
        (
            "Use this link to verify your email address. "
            "It expires in 24 hours.\n\n"
            f"{token_link(settings.EMAIL_VERIFICATION_URL, raw_token)}\n\n"
            "If you didn't create a Jivo account, "
            "you can ignore this email."
        ),
        user,
    )

    return raw_token


def verify_email(raw_token):
    """Mark the email verified with a token, or raise InvalidTokenError."""

    with transaction.atomic():

        verification_token = _use_token(
            EmailVerificationToken,
            raw_token,
            "verification",
        )

        user = verification_token.user
        user.is_verified = True

        user.save(
            update_fields=[
                "is_verified",
                "updated_at",
            ]
        )

        EmailVerificationToken.objects.filter(
            user=user,
            used_at__isnull=True,
        ).update(
            used_at=timezone.now()
        )
