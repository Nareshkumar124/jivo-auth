"""The JI dashboards, mounted at /dashboards/ (config/urls.py)."""

from django.urls import path

from . import views

app_name = "boards"

urlpatterns = [
    # Pages in the ticket system's layout.
    path("", views.IndexView.as_view(), name="index"),

    # The wall rotation, full window.
    path("carousel/", views.CarouselView.as_view(), name="carousel"),

    # Boards somebody composed in the factory's dashboard builder. Before the
    # fixed boards, and two segments deep, so "built" is never read as a key.
    path("built/<slug:slug>/", views.BuiltBoardView.as_view(), name="built"),
    path("built/<slug:slug>/screen/", views.BuiltScreenView.as_view(), name="built-screen"),
    path("built/<slug:slug>/fragment/", views.BuiltFragmentView.as_view(), name="built-fragment"),
    path("built/<slug:slug>/data/", views.BuiltDataView.as_view(), name="built-data"),

    path("<slug:key>/", views.BoardView.as_view(), name="board"),
    path("<slug:key>/screen/", views.ScreenView.as_view(), name="screen"),
    path("<slug:key>/fragment/", views.FragmentView.as_view(), name="fragment"),
    path("<slug:key>/data/", views.DataView.as_view(), name="data"),
]
