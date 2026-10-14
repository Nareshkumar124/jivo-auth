"""
JSON API of the support system, for applications that let users report a
problem without leaving the app. Authenticated with the user's access
token; the same permission rules as the portal apply. Attachments are
added in the portal.
"""

from rest_framework import generics, permissions, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from . import access, services
from .api_serializers import (
    CatalogDepartmentSerializer,
    CommentCreateSerializer,
    CommentSerializer,
    TicketCreateSerializer,
    TicketDetailSerializer,
    TicketListSerializer,
)
from .forms import catalog
from .models import Ticket
from .schema import (
    catalog_schema,
    ticket_comment_schema,
    ticket_detail_schema,
    ticket_list_schema,
)
from .services import SupportError


class TicketPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


@catalog_schema
class CatalogView(generics.ListAPIView):
    """Active departments and the applications in each."""

    serializer_class = CatalogDepartmentSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        from django.db.models import Prefetch

        departments, applications = catalog()

        return departments.prefetch_related(
            Prefetch("applications", queryset=applications, to_attr="active_applications")
        )


@ticket_list_schema
class TicketListCreateView(generics.ListCreateAPIView):

    permission_classes = [permissions.IsAuthenticated]
    pagination_class = TicketPagination

    def get_throttles(self):
        if self.request.method == "POST":
            self.throttle_scope = "support_ticket"

        return super().get_throttles()

    def get_serializer_class(self):
        return TicketCreateSerializer if self.request.method == "POST" else TicketListSerializer

    def get_queryset(self):
        scope = self.request.query_params.get("scope", "mine")

        if scope == "team" and (access.is_team_member(self.request.user) or access.is_admin(self.request.user)):
            queryset = access.visible_tickets(self.request.user)
        else:
            queryset = Ticket.objects.filter(created_by=self.request.user)

        status_filter = self.request.query_params.get("status")

        if status_filter == "active":
            queryset = queryset.active()
        elif status_filter in Ticket.Status.values:
            queryset = queryset.filter(status=status_filter)

        return queryset.with_related().order_by("-created_at")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        ticket = services.create_ticket(
            created_by=request.user,
            department=data["department"],
            application=data["application"],
            subject=data["subject"],
            description=data["description"],
        )

        ticket = Ticket.objects.with_related().get(pk=ticket.pk)
        output = TicketDetailSerializer(ticket, context={**self.get_serializer_context(), "internal_ok": False})

        return Response(output.data, status=status.HTTP_201_CREATED)


class TicketMixin:

    def get_ticket(self):
        if not hasattr(self, "_ticket"):
            try:
                ticket = Ticket.objects.with_related().get(pk=self.kwargs["pk"])
            except Ticket.DoesNotExist as exc:
                raise NotFound("No such ticket.") from exc

            if not access.can_view(self.request.user, ticket):
                # The same answer as for a missing ticket: no probing by ID.
                raise NotFound("No such ticket.")

            self._ticket = ticket

        return self._ticket


@ticket_detail_schema
class TicketDetailView(TicketMixin, generics.RetrieveAPIView):

    serializer_class = TicketDetailSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.get_ticket()

    def get_serializer_context(self):
        context = super().get_serializer_context()

        if "pk" in self.kwargs:
            context["internal_ok"] = access.can_view_internal(self.request.user, self.get_ticket())

        return context


@ticket_comment_schema
class TicketCommentView(TicketMixin, generics.GenericAPIView):

    serializer_class = CommentCreateSerializer
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "support_comment"

    def post(self, request, pk):
        ticket = self.get_ticket()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data["is_internal"] and not access.can_view_internal(request.user, ticket):
            raise PermissionDenied("Only the responsible team can add internal notes.")

        try:
            comment = services.add_comment(ticket, request.user, data["body"], internal=data["is_internal"])
        except SupportError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            CommentSerializer(comment, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )
