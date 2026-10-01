"""
Local-password sign-in for named emergency accounts only (decision D7), for
when Jivo Auth can't be reached.

    AUTHENTICATION_BACKENDS = [
        "jivo_auth.backends.JivoAuthBackend",
        "<app>.backends.BreakGlassBackend",
    ]
    BREAK_GLASS_USERNAMES = ["breakglass"]

Don't use plain ModelBackend instead: until cleanup, every migrated user
still has their old password hash locally, and ModelBackend would accept it.
"""

from django.conf import settings
from django.contrib.auth.backends import ModelBackend


class BreakGlassBackend(ModelBackend):

    def user_can_authenticate(self, user):
        allowed = getattr(settings, "BREAK_GLASS_USERNAMES", ())

        return super().user_can_authenticate(user) and user.get_username() in allowed
