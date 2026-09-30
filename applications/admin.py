from django.contrib import admin

from .models import Application


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):

    list_display = [
        "slug",
        "name",
        "is_active",
        "api_key_prefix",
        "api_key_created_at",
    ]

    list_filter = [
        "is_active",
    ]

    search_fields = [
        "slug",
        "name",
    ]

    autocomplete_fields = [
        "users",
    ]

    readonly_fields = [
        "api_key_prefix",
        "api_key_created_at",
        "created_at",
    ]

    fields = [
        "slug",
        "name",
        "is_active",
        "users",
        "api_key_prefix",
        "api_key_created_at",
        "created_at",
    ]
