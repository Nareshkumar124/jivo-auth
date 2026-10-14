"""
The boards app owns no data. Every figure it shows is read from the factory
application's databases at request time (``boards/sources``), so there is no
table here: only the permission that decides who sees the JI dashboards,
which Django can only mint against a model.
"""

from django.db import models


class Dashboard(models.Model):
    """A placeholder for the dashboards' permission; never a table.

    ``boards.view_dashboards`` is a normal Django permission, so it can be
    given in two places: in the ticket system (Administration → People and
    roles → dashboard access) and in the auth admin (/admin/ → Users → User
    permissions, or the "Dashboard viewer" staff role). Superusers always
    have it.
    """

    class Meta:
        managed = False
        default_permissions = ()
        verbose_name = "JI dashboard"
        verbose_name_plural = "JI dashboards"
        permissions = [
            ("view_dashboards", "Can see the JI dashboards"),
        ]
