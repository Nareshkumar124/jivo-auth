from django.urls import path

from .views import ApplicationUserListView


urlpatterns = [
    path(
        "users/",
        ApplicationUserListView.as_view(),
        name="application-users",
    ),
]
