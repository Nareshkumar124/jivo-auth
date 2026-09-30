from django.apps import AppConfig


class AuthenticationConfig(AppConfig):
    name = 'authentication'
    verbose_name = "Sign-in"

    def ready(self):
        from .tokens import install_token_backend

        install_token_backend()
