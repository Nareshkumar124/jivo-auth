from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError


def validate_password_strength(password, user=None):
    # Run the AUTH_PASSWORD_VALIDATORS from settings (common passwords,
    # similarity to the user's details, ...) before the composition rules.
    validate_password(password, user)

    if len(password) < 8:
        raise ValidationError(
            "Password must contain at least 8 characters."
        )

    if not any(char.isupper() for char in password):
        raise ValidationError(
            "Password must contain at least one uppercase letter."
        )

    if not any(char.islower() for char in password):
        raise ValidationError(
            "Password must contain at least one lowercase letter."
        )

    if not any(char.isdigit() for char in password):
        raise ValidationError(
            "Password must contain at least one number."
        )

    if not any(not char.isalnum() for char in password):
        raise ValidationError(
            "Password must contain at least one special character."
        )
