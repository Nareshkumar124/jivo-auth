from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response


@api_view(["GET"])
def api_whoami(request):
    return Response(
        {
            "id": str(request.user.pk),
            "email": request.user.email,
            "type": type(request.user).__name__,
        }
    )


@api_view(["GET"])
@permission_classes([IsAdminUser])
def api_admin_only(request):
    return Response({"ok": True})


@login_required
def whoami(request):
    return JsonResponse(
        {
            "username": request.user.get_username(),
            "email": request.user.email,
        }
    )


urlpatterns = [
    path("api/whoami/", api_whoami),
    path("api/admin-only/", api_admin_only),
    path("whoami/", whoami),
    path("login/", auth_views.LoginView.as_view()),
    path("logout/", auth_views.LogoutView.as_view()),
]
