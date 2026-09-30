from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm

from applications.models import Application
from authentication.services import revoke_all_sessions

from .models import User


class UserCreationForm(AdminUserCreationForm):

    class Meta:
        model = User
        fields = ("email", "is_verified")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["is_verified"].initial = True
        self.fields["is_verified"].label = "Email verified"
        self.fields["is_verified"].help_text = (
            "Untick if you haven't confirmed the address; the user then "
            "has to verify it before they can log in."
        )


class UserEditForm(UserChangeForm):

    class Meta:
        model = User
        fields = "__all__"


class ApplicationAccessInline(admin.TabularInline):
    model = Application.users.through
    extra = 0
    verbose_name = "application access"
    verbose_name_plural = "application access"


@admin.register(User)
class UserAdmin(DjangoUserAdmin):

    form = UserEditForm
    add_form = UserCreationForm

    list_display = [
        "email",
        "first_name",
        "last_name",
        "is_active",
        "is_verified",
        "is_staff",
        "created_at",
    ]

    list_filter = [
        "is_active",
        "is_verified",
        "is_staff",
        "applications",
    ]

    search_fields = [
        "email",
        "first_name",
        "last_name",
    ]

    ordering = [
        "email",
    ]

    readonly_fields = [
        "last_login",
        "created_at",
        "updated_at",
    ]

    fieldsets = [
        (None, {"fields": ["email", "password"]}),
        ("Profile", {"fields": ["first_name", "last_name"]}),
        (
            "Status",
            {
                "fields": [
                    "is_active",
                    "is_verified",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ],
            },
        ),
        ("Dates", {"fields": ["last_login", "created_at", "updated_at"]}),
    ]

    add_fieldsets = [
        (
            None,
            {
                "classes": ["wide"],
                "fields": [
                    "email",
                    "is_verified",
                    "usable_password",
                    "password1",
                    "password2",
                ],
            },
        ),
    ]

    inlines = [
        ApplicationAccessInline,
    ]

    actions = [
        "log_out_everywhere",
    ]

    @admin.action(
        description="Log out selected users everywhere",
        permissions=["change"],
    )
    def log_out_everywhere(self, request, queryset):
        count = sum(revoke_all_sessions(user) for user in queryset)

        self.message_user(request, f"Revoked {count} session(s).")
