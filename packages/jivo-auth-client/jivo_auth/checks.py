from django.conf import settings
from django.core.checks import Error, Warning

from . import settings as conf


def check_settings(app_configs, **kwargs):
    errors = []

    unknown = conf.unknown_settings()

    if unknown:
        errors.append(
            Error(
                f"Unknown JIVO_AUTH settings: {', '.join(unknown)}.",
                hint=f"Valid settings: {', '.join(conf.DEFAULTS)}.",
                id="jivo_auth.E001",
            )
        )

    algorithm = conf.algorithm()

    if algorithm.startswith("HS"):
        if not conf.get("SECRET"):
            errors.append(
                Error(
                    f"JIVO_AUTH['SECRET'] is required for {algorithm}.",
                    id="jivo_auth.E002",
                )
            )

    elif not conf.get("PUBLIC_KEY") and not conf.jwks_url():
        errors.append(
            Error(
                "Tokens can't be verified: no key source is configured.",
                hint=(
                    "Set JIVO_AUTH['URL'] to Jivo Auth's base URL, e.g. "
                    "https://auth.jivo.in."
                ),
                id="jivo_auth.E002",
            )
        )

    if not conf.get("APP"):
        if conf.get("ALLOW_ALL_USERS"):
            errors.append(
                Warning(
                    "JIVO_AUTH['ALLOW_ALL_USERS'] is set, so every Jivo Auth "
                    "user can use this application.",
                    hint=(
                        "Set JIVO_AUTH['APP'] to this application's slug so "
                        "only users granted access get in."
                    ),
                    id="jivo_auth.W001",
                )
            )
        else:
            errors.append(
                Error(
                    "JIVO_AUTH['APP'] is not set, so every user is refused.",
                    hint=(
                        "Set it to this application's slug in Jivo Auth, or "
                        "set JIVO_AUTH['ALLOW_ALL_USERS'] = True to accept "
                        "every Jivo Auth user."
                    ),
                    id="jivo_auth.E003",
                )
            )

    if (
        "jivo_auth.backends.JivoAuthBackend" in settings.AUTHENTICATION_BACKENDS
        and not conf.get("API_KEY")
    ):
        errors.append(
            Warning(
                "JivoAuthBackend is used without JIVO_AUTH['API_KEY'].",
                hint=(
                    "Without the key, Jivo Auth counts every login through "
                    "this application against this server's IP address, so "
                    "a few wrong passwords lock everyone out for a minute."
                ),
                id="jivo_auth.W002",
            )
        )

    return errors
