import uuid

from rest_framework import generics
from rest_framework.exceptions import ValidationError

from users.models import User

from .authentication import ApplicationKeyAuthentication, IsApplication
from .schema import application_users_schema
from .serializers import MAX_USER_IDS, ApplicationUserSerializer
from .throttling import ScopedRateThrottle


@application_users_schema
class ApplicationUserListView(generics.ListAPIView):

    serializer_class = ApplicationUserSerializer

    authentication_classes = [
        ApplicationKeyAuthentication,
    ]

    permission_classes = [
        IsApplication,
    ]

    # Only the per-application rate; the per-user rate is meant for people.
    throttle_classes = [
        ScopedRateThrottle,
    ]

    throttle_scope = "application"

    def get_queryset(self):

        queryset = User.objects.filter(
            applications=self.request.auth,
        ).order_by("email")

        raw_ids = self.request.query_params.getlist("id")

        if not raw_ids:
            return queryset

        if len(raw_ids) > MAX_USER_IDS:
            raise ValidationError(
                {
                    "id": [f"At most {MAX_USER_IDS} IDs per request."],
                }
            )

        try:
            ids = [uuid.UUID(raw_id) for raw_id in raw_ids]
        except ValueError as exc:
            raise ValidationError(
                {
                    "id": ["Each ID must be a UUID."],
                }
            ) from exc

        return queryset.filter(id__in=ids)
