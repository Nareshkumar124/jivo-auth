"""The ticket system, mounted at / (config/urls.py includes it last)."""

from django.urls import path
from django.views.generic import RedirectView

from . import manage_views, views


app_name = "support"

urlpatterns = [
    # Visitors get the sign-in page here, everyone else the dashboard.
    path("", views.HomeView.as_view(), name="home"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("password/", views.PasswordChangeView.as_view(), name="password-change"),

    path("tickets/", views.MyTicketsView.as_view(), name="my-tickets"),
    path("tickets/new/", views.TicketCreateView.as_view(), name="ticket-create"),
    path("tickets/<int:pk>/", views.TicketDetailView.as_view(), name="ticket-detail"),
    path("tickets/<int:pk>/comment/", views.CommentView.as_view(), name="ticket-comment"),
    path("tickets/<int:pk>/claim/", views.ClaimView.as_view(), name="ticket-claim"),
    path("tickets/<int:pk>/assign/", views.AssignView.as_view(), name="ticket-assign"),
    path("tickets/<int:pk>/status/", views.StatusView.as_view(), name="ticket-status"),
    path("tickets/<int:pk>/priority/", views.PriorityView.as_view(), name="ticket-priority"),
    path("tickets/<int:pk>/team/", views.TeamView.as_view(), name="ticket-team"),
    path("tickets/<int:pk>/resolve/", views.ResolveView.as_view(), name="ticket-resolve"),
    path("tickets/<int:pk>/close/", views.CloseView.as_view(), name="ticket-close"),
    path("tickets/<int:pk>/reopen/", views.ReopenView.as_view(), name="ticket-reopen"),

    path("attachments/<uuid:pk>/", views.AttachmentView.as_view(), name="attachment"),

    path("team/", views.TeamIndexView.as_view(), name="team-index"),
    path("team/<int:pk>/", views.TeamOverviewView.as_view(), name="team-overview"),
    path("team/<int:pk>/reassign/", views.TeamReassignView.as_view(), name="team-reassign"),
    path("team/<slug:preset>/", views.TeamTicketsView.as_view(), name="team-tickets"),

    path("notifications/", views.NotificationsView.as_view(), name="notifications"),

    # Ticket administrators only.
    path("manage/", RedirectView.as_view(pattern_name="support:manage-tickets"), name="manage"),
    path("manage/tickets/", manage_views.ManageTicketsView.as_view(), name="manage-tickets"),
    path("manage/tickets/bulk/", manage_views.BulkActionView.as_view(), name="manage-bulk"),
    path("manage/departments/", manage_views.DepartmentListView.as_view(), name="manage-departments"),
    path("manage/departments/new/", manage_views.DepartmentFormView.as_view(), name="manage-department-create"),
    path("manage/departments/<int:pk>/", manage_views.DepartmentFormView.as_view(), name="manage-department"),
    path("manage/departments/<int:pk>/toggle/", manage_views.DepartmentToggleView.as_view(), name="manage-department-toggle"),
    path("manage/applications/", manage_views.ApplicationListView.as_view(), name="manage-applications"),
    path("manage/applications/new/", manage_views.ApplicationCreateView.as_view(), name="manage-application-create"),
    path("manage/applications/<uuid:pk>/", manage_views.ApplicationEditView.as_view(), name="manage-application"),
    path("manage/teams/", manage_views.TeamListView.as_view(), name="manage-teams"),
    path("manage/teams/new/", manage_views.TeamCreateView.as_view(), name="manage-team-create"),
    path("manage/teams/<int:pk>/", manage_views.TeamDetailView.as_view(), name="manage-team"),
    path("manage/teams/<int:pk>/members/", manage_views.MemberAddView.as_view(), name="manage-team-member-add"),
    path("manage/teams/<int:pk>/members/<int:membership_id>/", manage_views.MemberUpdateView.as_view(), name="manage-team-member"),
    path("manage/people/", manage_views.PeopleView.as_view(), name="manage-people"),
    path("manage/people/administrators/", manage_views.AdministratorView.as_view(), name="manage-administrators"),
    path("manage/people/dashboards/", manage_views.DashboardAccessView.as_view(), name="manage-dashboard-access"),
    path("manage/reports/", manage_views.ReportsView.as_view(), name="manage-reports"),
    path("manage/activity/", manage_views.ActivityView.as_view(), name="manage-activity"),
    path("manage/settings/", manage_views.SettingsView.as_view(), name="manage-settings"),
]
