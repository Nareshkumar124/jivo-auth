"""OpenAPI documentation for the support endpoints."""

from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import serializers

from config.openapi import (
    AUTH_REQUIRED,
    UNAUTHORIZED_RESPONSE,
    USER_ID_EXAMPLE,
    error_response,
    example,
    rate_limit,
    throttled_response,
    validation_error_response,
)

from .api_serializers import (
    CatalogDepartmentSerializer,
    CommentCreateSerializer,
    CommentSerializer,
    TicketCreateSerializer,
    TicketDetailSerializer,
    TicketListSerializer,
)


PERSON_EXAMPLE = {"id": USER_ID_EXAMPLE, "email": "alice@jivo.in", "name": "Alice Smith"}

TICKET_EXAMPLE = {
    "id": 42,
    "reference": "JT-0042",
    "subject": "Can't download the invoice PDF",
    "status": "in_progress",
    "priority": "medium",
    "department": "IT Department",
    "application": "oms",
    "team": "OMS Team",
    "assignee": {"id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "email": "ravi@jivo.in", "name": "Ravi Kumar"},
    "created_by": PERSON_EXAMPLE,
    "created_at": "2026-10-13T06:07:16.357563Z",
    "updated_at": "2026-10-13T06:40:02.112001Z",
    "resolved_at": None,
    "closed_at": None,
    "resolve_due_at": "2026-10-16T06:07:16.357563Z",
    "url": "https://auth.jivo.in/tickets/42/",
}

COMMENT_EXAMPLE = {
    "id": 7,
    "author": {"id": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "email": "ravi@jivo.in", "name": "Ravi Kumar"},
    "body": "Which browser are you using? Does it happen on another computer?",
    "is_internal": False,
    "is_info_request": True,
    "created_at": "2026-10-13T06:40:02.112001Z",
    "attachments": [],
}

TICKET_DETAIL_EXAMPLE = {
    **TICKET_EXAMPLE,
    "description": "Clicking “Download PDF” on order 1042 shows a blank page.",
    "resolution": "",
    "reopen_count": 0,
    "comments": [COMMENT_EXAMPLE],
    "attachments": [
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "original_name": "blank-page.png",
            "content_type": "image/png",
            "size": 48213,
            "created_at": "2026-10-13T06:07:16.401000Z",
            "url": "https://auth.jivo.in/attachments/3fa85f64-5717-4562-b3fc-2c963f66afa6/",
        }
    ],
}

CATALOG_EXAMPLE = {
    "id": 1,
    "name": "IT Department",
    "description": "Internal applications.",
    "applications": [
        {"id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "slug": "oms", "name": "Jivo OMS"},
    ],
}


class PaginatedTicketsSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = TicketListSerializer(many=True)


NOT_FOUND_RESPONSE = error_response(
    "No such ticket, or the user may not see it.",
    example("Not found", {"detail": "No such ticket."}),
)


catalog_schema = extend_schema(
    tags=["Support"],
    summary="List departments and applications",
    description=f"""
The choices for a new ticket: every active department with the active
applications in it. Use the department `id` and the application `slug`
when creating a ticket.

{AUTH_REQUIRED}
""",
    responses={
        200: OpenApiResponse(
            CatalogDepartmentSerializer(many=True),
            description="Departments, alphabetically, each with its applications.",
            examples=[example("Catalog", CATALOG_EXAMPLE)],
        ),
        401: UNAUTHORIZED_RESPONSE,
    },
)


ticket_list_schema = extend_schema_view(
    get=extend_schema(
        tags=["Support"],
        summary="List my tickets",
        description=f"""
The user's own tickets, newest first, 25 per page. Team members and
support administrators can pass `scope=team` to also list the tickets
routed to their teams (administrators: every ticket).

{AUTH_REQUIRED}
""",
        parameters=[
            OpenApiParameter("scope", str, OpenApiParameter.QUERY, description="`mine` (default) or `team`.", enum=["mine", "team"]),
            OpenApiParameter("status", str, OpenApiParameter.QUERY, description="A status, or `active` for everything not resolved or closed."),
            OpenApiParameter("page", int, OpenApiParameter.QUERY),
            OpenApiParameter("page_size", int, OpenApiParameter.QUERY, description="Up to 100."),
        ],
        responses={
            200: OpenApiResponse(
                PaginatedTicketsSerializer,
                description="A page of tickets.",
                examples=[example("Tickets", {"count": 1, "next": None, "previous": None, "results": [TICKET_EXAMPLE]})],
            ),
            401: UNAUTHORIZED_RESPONSE,
        },
    ),
    post=extend_schema(
        tags=["Support"],
        summary="Create a ticket",
        description=f"""
Report a problem with an application. The ticket is routed to the team
responsible for the application and the team is notified. Attachments can
be added afterwards in the ticket system (the `url` in the response).

Rate limited to {rate_limit("support_ticket")} per user.

{AUTH_REQUIRED}
""",
        request=TicketCreateSerializer,
        responses={
            201: OpenApiResponse(
                TicketDetailSerializer,
                description="The new ticket.",
                examples=[example("Created", {**TICKET_DETAIL_EXAMPLE, "status": "open", "assignee": None, "comments": []})],
            ),
            400: validation_error_response(
                "Invalid input.",
                example("Wrong department", {"application": ["The application doesn't belong to that department."]}),
            ),
            401: UNAUTHORIZED_RESPONSE,
            429: throttled_response("support_ticket"),
        },
        examples=[
            OpenApiExample(
                "New ticket",
                value={
                    "department": 1,
                    "application": "oms",
                    "subject": "Can't download the invoice PDF",
                    "description": "Clicking “Download PDF” on order 1042 shows a blank page.",
                },
                request_only=True,
            ),
        ],
    ),
)


ticket_detail_schema = extend_schema(
    tags=["Support"],
    summary="Get a ticket",
    description=f"""
A ticket with its conversation and attachments. Internal notes are only
included for the responsible team and administrators. Tickets the user
may not see answer `404`.

{AUTH_REQUIRED}
""",
    responses={
        200: OpenApiResponse(
            TicketDetailSerializer,
            description="The ticket.",
            examples=[example("Ticket", TICKET_DETAIL_EXAMPLE)],
        ),
        401: UNAUTHORIZED_RESPONSE,
        404: NOT_FOUND_RESPONSE,
    },
)


ticket_comment_schema = extend_schema(
    tags=["Support"],
    summary="Reply on a ticket",
    description=f"""
Add a message to the conversation. A user's reply on a ticket that is
waiting for information puts it back in progress. Team members may post
an internal note with `is_internal`.

Rate limited to {rate_limit("support_comment")} per user.

{AUTH_REQUIRED}
""",
    request=CommentCreateSerializer,
    responses={
        201: OpenApiResponse(
            CommentSerializer,
            description="The new comment.",
            examples=[example("Reply", {**COMMENT_EXAMPLE, "is_info_request": False, "body": "Chrome 130, and yes, on two computers."})],
        ),
        400: error_response(
            "The ticket is closed, or the input is invalid.",
            example("Closed", {"detail": "This ticket is closed. Reopen it to continue the conversation."}),
        ),
        401: UNAUTHORIZED_RESPONSE,
        403: error_response(
            "Internal notes need the responsible team.",
            example("Forbidden", {"detail": "Only the responsible team can add internal notes."}),
        ),
        404: NOT_FOUND_RESPONSE,
        429: throttled_response("support_comment"),
    },
    examples=[
        OpenApiExample(
            "Reply",
            value={"body": "Chrome 130, and yes, on two computers.", "is_internal": False},
            request_only=True,
        ),
    ],
)
