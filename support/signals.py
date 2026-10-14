"""Housekeeping signals: delete an attachment's file with its row."""

from django.db.models.signals import post_delete


def delete_attachment_file(sender, instance, **kwargs):
    if instance.file:
        instance.file.delete(save=False)


def connect():
    from .models import TicketAttachment

    post_delete.connect(
        delete_attachment_file,
        sender=TicketAttachment,
        dispatch_uid="support.delete_attachment_file",
    )
