"""
Default staff roles (Django groups). Created once, after migrations, if they
don't exist; administrators may then change them freely. None can grant
staff or superuser status: only superusers can.
"""

DEFAULT_ROLES = {
    "Support": {
        "description": "Help users: unlock, verify, reset, end sessions.",
        "permissions": [
            "users.view_user",
            "users.change_user",
            "authentication.view_usersession",
            "authentication.change_usersession",
            "applications.view_application",
            "audit.view_auditevent",
        ],
    },
    "Application manager": {
        "description": "Register applications, rotate keys, grant access.",
        "permissions": [
            "applications.view_application",
            "applications.add_application",
            "applications.change_application",
            "users.view_user",
            "audit.view_auditevent",
        ],
    },
    "Auditor": {
        "description": "Read-only access to everything, including the audit log.",
        "permissions": [
            "users.view_user",
            "adminpanel.view_staffrole",
            "applications.view_application",
            "authentication.view_usersession",
            "audit.view_auditevent",
        ],
    },
}


def create_default_roles(sender, using="default", **kwargs):
    from django.apps import apps
    from django.contrib.auth.management import create_permissions
    from django.contrib.auth.models import Group, Permission

    # post_migrate fires once per app; act once.
    if getattr(sender, "label", None) != "adminpanel":
        return

    # adminpanel is the first app, so on a fresh database the other apps'
    # permissions don't exist yet. Creating them is idempotent.
    for app_config in apps.get_app_configs():
        create_permissions(app_config, verbosity=0, using=using)

    for name, role in DEFAULT_ROLES.items():
        group, created = Group.objects.using(using).get_or_create(name=name)

        if not created:
            continue

        for dotted in role["permissions"]:
            app_label, codename = dotted.split(".")

            permission = Permission.objects.using(using).filter(
                content_type__app_label=app_label,
                codename=codename,
            ).first()

            if permission is not None:
                group.permissions.add(permission)
