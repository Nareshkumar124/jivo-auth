from django.urls import path

from .api import CatalogView, TicketCommentView, TicketDetailView, TicketListCreateView


urlpatterns = [
    path("catalog/", CatalogView.as_view(), name="support-catalog"),
    path("tickets/", TicketListCreateView.as_view(), name="support-tickets"),
    path("tickets/<int:pk>/", TicketDetailView.as_view(), name="support-ticket"),
    path("tickets/<int:pk>/comments/", TicketCommentView.as_view(), name="support-ticket-comments"),
]
