from django.contrib.auth.models import Group


class StaffRole(Group):
    """
    Django's Group, presented as a staff role in the admin panel. A proxy:
    the same table, only its own name and admin permissions.
    """

    class Meta:
        proxy = True
        verbose_name = "staff role"
        verbose_name_plural = "staff roles"
