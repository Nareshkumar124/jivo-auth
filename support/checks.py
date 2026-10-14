"""
Deployment checks of the support app (`manage.py check --deploy`).

deploy.sh runs them with the new image, the real volume mounts and the
container's user before switching, so a deploy whose attachments couldn't
be saved stops there instead of failing on the first upload.
"""

import os
from pathlib import Path

from django.conf import settings
from django.core.checks import Error, register


@register("support", deploy=True)
def media_root_writable(app_configs=None, **kwargs):
    root = Path(settings.MEDIA_ROOT)

    if not root.is_dir():
        return [
            Error(
                f"MEDIA_ROOT ({root}) doesn't exist, so support ticket attachments can't be saved.",
                hint=(
                    "On the server, create media/ next to docker-compose.yml, owned by the "
                    "container's user (APP_UID, default 10001). See docs/deploy-auth-service.md."
                ),
                id="support.E001",
            )
        ]

    if not os.access(root, os.W_OK | os.X_OK):
        return [
            Error(
                f"MEDIA_ROOT ({root}) isn't writable by this process (uid {os.getuid()}).",
                hint="chown the directory to the container's user (APP_UID, default 10001).",
                id="support.E002",
            )
        ]

    return []
