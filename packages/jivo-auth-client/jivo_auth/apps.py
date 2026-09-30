from django.apps import AppConfig
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.core import checks


class JivoAuthConfig(AppConfig):
    name = "jivo_auth"
    verbose_name = "Jivo Auth"

    def ready(self):
        from .checks import check_settings
        from .signals import end_remote_session_on_logout, store_tokens_on_login

        checks.register(check_settings)

        user_logged_in.connect(
            store_tokens_on_login,
            dispatch_uid="jivo_auth.store_tokens_on_login",
        )

        user_logged_out.connect(
            end_remote_session_on_logout,
            dispatch_uid="jivo_auth.end_remote_session_on_logout",
        )
