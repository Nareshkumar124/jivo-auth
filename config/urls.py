"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path,include

from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from authentication.views import JWKSView
from users.pages import ResetPasswordPage, VerifyEmailPage

urlpatterns = [
    path('admin/', admin.site.urls),

    # Health Check app
    path("api/v1/health/",include("health.urls"),),

    # User app
    path("api/v1/users/",include("users.urls"),),

    # Authentication App
    path("api/v1/auth/",include("authentication.urls"),),

    # Server-to-server endpoints for applications
    path("api/v1/apps/",include("applications.urls"),),

    # Public keys that verify issued tokens
    path(".well-known/jwks.json",JWKSView.as_view(),name="jwks",),

    # Pages linked from emails
    path("reset-password/",ResetPasswordPage.as_view(),name="reset-password-page",),
    path("verify-email/",VerifyEmailPage.as_view(),name="verify-email-page",),

    # OpenAPI
    path("api/schema/",SpectacularAPIView.as_view(),name="schema",),
    path("api/docs/",SpectacularSwaggerView.as_view(url_name="schema",),name="swagger-ui",),
    path("api/redoc/",SpectacularRedocView.as_view(url_name="schema"),name="redoc",),

]
