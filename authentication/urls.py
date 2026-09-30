from django.urls import path

from .views import (
    LoginView,
    LogoutView,
    RefreshTokenView,
    RevokeAllSessionsView,
    RevokeSessionView,
    SessionListView,
    VerifyTokenView,
)


urlpatterns = [

    path(
        "login/",
        LoginView.as_view(),
        name="login",
    ),

    path(
        "refresh/",
        RefreshTokenView.as_view(),
        name="refresh",
    ),

    path(
        "verify/",
        VerifyTokenView.as_view(),
        name="verify",
    ),

    path(
        "logout/",
        LogoutView.as_view(),
        name="logout",
    ),

    path(
        "sessions/",
        SessionListView.as_view(),
        name="sessions",
    ),

    path(
        "sessions/<uuid:pk>/revoke/",
        RevokeSessionView.as_view(),
        name="revoke-session",
    ),

    path(
        "sessions/revoke-all/",
        RevokeAllSessionsView.as_view(),
        name="revoke-all-sessions",
    ),
]