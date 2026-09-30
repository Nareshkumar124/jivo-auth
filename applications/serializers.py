from rest_framework import serializers

from users.models import User


# Most user IDs one lookup request may ask for.
MAX_USER_IDS = 100


class ApplicationUserSerializer(serializers.ModelSerializer):

    class Meta:
        model = User

        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "employee_code",
            "is_active",
        ]

        read_only_fields = fields

        extra_kwargs = {
            "id": {
                "help_text": "User ID; the `sub` claim of the user's tokens.",
            },
            "employee_code": {
                "help_text": "The person's code in Jivo's HR records; empty if not set.",
            },
            "is_active": {
                "help_text": "Inactive users can't log in or refresh tokens.",
            },
        }
