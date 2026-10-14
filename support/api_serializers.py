"""Serializers of the support API (used by applications on the user's behalf)."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from applications.models import Application

from .models import Department, Ticket, TicketAttachment, TicketComment


class CatalogApplicationSerializer(serializers.ModelSerializer):

    class Meta:
        model = Application
        fields = ["id", "slug", "name"]


class CatalogDepartmentSerializer(serializers.ModelSerializer):

    applications = serializers.SerializerMethodField(
        help_text="Active applications in the department.",
    )

    class Meta:
        model = Department
        fields = ["id", "name", "description", "applications"]

    @extend_schema_field(CatalogApplicationSerializer(many=True))
    def get_applications(self, department):
        # Set by CatalogView's prefetch; falls back to a query.
        applications = getattr(department, "active_applications", None)

        if applications is None:
            applications = department.applications.filter(is_active=True).order_by("name")

        return CatalogApplicationSerializer(applications, many=True).data


class PersonSerializer(serializers.Serializer):

    id = serializers.UUIDField(read_only=True)
    email = serializers.EmailField(read_only=True)
    name = serializers.SerializerMethodField()

    @extend_schema_field(serializers.CharField())
    def get_name(self, user):
        return user.get_full_name()


class AttachmentSerializer(serializers.ModelSerializer):

    url = serializers.SerializerMethodField(help_text="Download URL in the ticket system (session sign-in).")

    class Meta:
        model = TicketAttachment
        fields = ["id", "original_name", "content_type", "size", "created_at", "url"]

    @extend_schema_field(serializers.URLField())
    def get_url(self, attachment):
        request = self.context.get("request")
        path = attachment.get_absolute_url()

        return request.build_absolute_uri(path) if request else path


class CommentSerializer(serializers.ModelSerializer):

    author = PersonSerializer(read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = TicketComment
        fields = ["id", "author", "body", "is_internal", "is_info_request", "created_at", "attachments"]
        read_only_fields = fields


class CommentCreateSerializer(serializers.Serializer):

    body = serializers.CharField(help_text="The message.")

    is_internal = serializers.BooleanField(
        default=False,
        help_text="An internal note, visible to the responsible team only. Team members and administrators only.",
    )


class TicketListSerializer(serializers.ModelSerializer):

    reference = serializers.CharField(read_only=True, help_text="Human-readable ticket ID, e.g. `JT-0042`.")
    department = serializers.CharField(source="department.name", read_only=True)
    application = serializers.CharField(source="application.slug", read_only=True)
    team = serializers.CharField(source="team.name", read_only=True, allow_null=True, default=None)
    assignee = PersonSerializer(read_only=True, allow_null=True)
    created_by = PersonSerializer(read_only=True, allow_null=True)
    url = serializers.SerializerMethodField(help_text="The ticket's page in the ticket system.")

    class Meta:
        model = Ticket
        fields = [
            "id", "reference", "subject", "status", "priority", "department", "application", "team",
            "assignee", "created_by", "created_at", "updated_at", "resolved_at", "closed_at",
            "resolve_due_at", "url",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.URLField())
    def get_url(self, ticket):
        request = self.context.get("request")
        path = ticket.get_absolute_url()

        return request.build_absolute_uri(path) if request else path


class TicketDetailSerializer(TicketListSerializer):

    comments = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()

    class Meta(TicketListSerializer.Meta):
        fields = [*TicketListSerializer.Meta.fields, "description", "resolution", "reopen_count", "comments", "attachments"]
        read_only_fields = fields

    def _internal_ok(self):
        return self.context.get("internal_ok", False)

    @extend_schema_field(CommentSerializer(many=True))
    def get_comments(self, ticket):
        comments = ticket.comments.select_related("author").prefetch_related("attachments")

        if not self._internal_ok():
            comments = comments.filter(is_internal=False)

        return CommentSerializer(comments, many=True, context=self.context).data

    @extend_schema_field(AttachmentSerializer(many=True))
    def get_attachments(self, ticket):
        attachments = ticket.attachments.filter(comment__isnull=True)

        if not self._internal_ok():
            attachments = attachments.filter(is_internal=False)

        return AttachmentSerializer(attachments, many=True, context=self.context).data


class TicketCreateSerializer(serializers.Serializer):

    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.filter(is_active=True),
        help_text="ID of an active department (see the catalog).",
    )

    application = serializers.SlugRelatedField(
        slug_field="slug",
        queryset=Application.objects.filter(is_active=True, tickets_enabled=True, department__is_active=True),
        help_text="Slug of an active application in that department.",
    )

    subject = serializers.CharField(max_length=200)

    description = serializers.CharField()

    def validate(self, attrs):
        if attrs["application"].department_id != attrs["department"].pk:
            raise serializers.ValidationError(
                {"application": ["The application doesn't belong to that department."]}
            )

        return attrs
