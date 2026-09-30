from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from applications.models import app_slugs

from .models import User
from .validators import validate_password_strength


def check_password_strength(password, user, field):
    try:
        validate_password_strength(password, user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(
            {
                field: list(exc.messages)
            }
        ) from exc


class UserSerializer(serializers.ModelSerializer):

    apps = serializers.SerializerMethodField(
        help_text=(
            "Slugs of the applications the user may use; the `apps` claim "
            "of new tokens."
        ),
    )

    class Meta:
        model = User

        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "employee_code",
            "is_active",
            "is_verified",
            "apps",
            "created_at",
            "updated_at",
        ]

        read_only_fields = [
            "id",
            "email",
            "employee_code",
            "is_active",
            "is_verified",
            "created_at",
            "updated_at",
        ]

        extra_kwargs = {
            "id": {
                "help_text": "User ID; the `sub` claim of issued tokens.",
            },
            "email": {
                "help_text": "Login email, stored lowercased. Read-only.",
            },
            "employee_code": {
                "help_text": (
                    "The person's code in Jivo's HR records, uppercase; empty "
                    "if not set. Read-only: set by an administrator."
                ),
            },
            "is_active": {
                "help_text": "Inactive accounts can't log in or refresh tokens.",
            },
            "is_verified": {
                "help_text": "Whether the email address has been verified.",
            },
        }

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_apps(self, user):
        return app_slugs(user)



class RegisterSerializer(serializers.ModelSerializer):

    password = serializers.CharField(
        write_only=True,
        min_length=8,
        help_text="See the password rules in the endpoint description.",
    )

    password_confirm = serializers.CharField(
        write_only=True,
        help_text="Must match `password`.",
    )


    
    class Meta:
        model = User

        fields = [
            "email",
            "password",
            "password_confirm",
            "first_name",
            "last_name",
        ]

        extra_kwargs = {
            "email": {
                "help_text": "Case-insensitive; stored lowercased.",
            },
        }

    def validate_email(self, value):
        value = value.lower().strip()

        allowed_domains = settings.REGISTRATION_EMAIL_DOMAINS

        if (
            allowed_domains
            and value.rsplit("@", 1)[-1] not in allowed_domains
        ):
            raise serializers.ValidationError(
                "Registration is limited to "
                + ", ".join(f"@{domain}" for domain in allowed_domains)
                + " email addresses."
            )

        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "A user with this email already exists."
            )

        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError(
                {
                    "password": "Passwords do not match."
                }
            )

        # Unsaved user, so the similarity validator can compare against it.
        candidate = User(
            email=attrs["email"],
            first_name=attrs.get("first_name", ""),
            last_name=attrs.get("last_name", ""),
        )

        check_password_strength(
            attrs["password"],
            candidate,
            "password",
        )

        return attrs

    def create(self, validated_data):
        validated_data.pop("password_confirm")

        password = validated_data.pop("password")

        try:
            user = User.objects.create_user( # type: ignore
                password=password,
                **validated_data,
            )

        # Lost a race with a concurrent registration for the same email.
        except IntegrityError as exc:
            raise serializers.ValidationError(
                {
                    "email": "A user with this email already exists."
                }
            ) from exc

        return user


class ChangePasswordSerializer(serializers.Serializer):

    current_password = serializers.CharField(
        write_only=True,
        help_text="The user's current password.",
    )

    new_password = serializers.CharField(
        write_only=True,
        min_length=8,
        help_text="See the password rules in the endpoint description.",
    )

    new_password_confirm = serializers.CharField(
        write_only=True,
        help_text="Must match `new_password`.",
    )

    def validate(self, attrs):

        user = self.context["request"].user

        if not user.check_password(
            attrs["current_password"]
        ):
            raise serializers.ValidationError(
                {
                    "current_password": "Current password is incorrect."
                }
            )

        if (
            attrs["new_password"]
            != attrs["new_password_confirm"]
        ):
            raise serializers.ValidationError(
                {
                    "new_password": "Passwords do not match."
                }
            )

        check_password_strength(
            attrs["new_password"],
            user,
            "new_password",
        )

        if user.check_password(
            attrs["new_password"]
        ):
            raise serializers.ValidationError(
                {
                    "new_password": (
                        "New password must be different "
                        "from the current password."
                    )
                }
            )

        return attrs


class ForgotPasswordSerializer(serializers.Serializer):

    email = serializers.EmailField(
        help_text="Email of the account to reset. Case-insensitive.",
    )



class ResetPasswordSerializer(serializers.Serializer):

    token = serializers.CharField(
        write_only=True,
        help_text="Reset token from the password reset email.",
    )

    new_password = serializers.CharField(
        write_only=True,
        min_length=8,
        help_text="See the password rules in the endpoint description.",
    )

    new_password_confirm = serializers.CharField(
        write_only=True,
        help_text="Must match `new_password`.",
    )

    def validate(self, attrs):

        if (
            attrs["new_password"]
            != attrs["new_password_confirm"]
        ):
            raise serializers.ValidationError(
                {
                    "new_password": "Passwords do not match."
                }
            )

        check_password_strength(
            attrs["new_password"],
            None,
            "new_password",
        )

        return attrs


class VerifyEmailSerializer(serializers.Serializer):

    token = serializers.CharField(
        write_only=True,
        help_text="Verification token from the email verification link.",
    )


class ResendVerificationSerializer(serializers.Serializer):

    email = serializers.EmailField(
        help_text="Email of the account to verify. Case-insensitive.",
    )
