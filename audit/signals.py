"""
Events recorded from model signals, so every path (API, admin, shell)
leaves the same trail.
"""

from django.contrib.auth import get_user_model
from django.db.models.signals import m2m_changed, post_save

from applications.models import Application

from .context import current_request
from .models import AuditEvent
from .services import _request_actor, record


def account_created(sender, instance, created, raw=False, **kwargs):

    if not created or raw:
        return

    request = current_request()

    if request is None:
        source = "system"
    elif _request_actor(request) is not None:
        source = "admin"
    else:
        source = "registration"

    record(
        AuditEvent.Type.ACCOUNT_CREATED,
        user=instance,
        source=source,
    )


def application_access_changed(sender, instance, action, reverse, pk_set, **kwargs):
    """Application.users changed, from either side of the relation."""

    if action == "pre_clear":
        # clear() passes no pk_set afterwards, so note what's there now.
        related = instance.applications if reverse else instance.users
        instance._audit_cleared_pks = set(related.values_list("pk", flat=True))
        return

    if action == "post_clear":
        pk_set = getattr(instance, "_audit_cleared_pks", set())
        event_type = AuditEvent.Type.ACCESS_REVOKED
    elif action == "post_add":
        event_type = AuditEvent.Type.ACCESS_GRANTED
    elif action == "post_remove":
        event_type = AuditEvent.Type.ACCESS_REVOKED
    else:
        return

    if not pk_set:
        return

    User = get_user_model()

    if reverse:
        users = [instance]
        applications = Application.objects.filter(pk__in=pk_set)
    else:
        users = User.objects.filter(pk__in=pk_set)
        applications = [instance]

    for user in users:
        for application in applications:
            record(
                event_type,
                user=user,
                application=application,
            )


def connect():
    post_save.connect(
        account_created,
        sender=get_user_model(),
        dispatch_uid="audit.account_created",
    )

    m2m_changed.connect(
        application_access_changed,
        sender=Application.users.through,
        dispatch_uid="audit.application_access_changed",
    )
