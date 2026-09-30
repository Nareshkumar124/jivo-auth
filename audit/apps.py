from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = 'audit'
    verbose_name = "Security"

    def ready(self):
        from .signals import connect

        connect()
