import uuid
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password

from . import settings as conf


@dataclass
class JivoUser:
    """
    request.user built from token claims alone, without a database. It has
    no staff status or permissions; use LOCAL_USERS for those.
    """

    id: uuid.UUID
    email: str = ""
    apps: tuple = ()
    claims: dict = field(default_factory=dict, repr=False)

    is_active = True
    is_staff = False
    is_superuser = False

    @classmethod
    def from_claims(cls, claims):
        return cls(
            id=uuid.UUID(str(claims["sub"])),
            email=claims.get("email") or "",
            apps=tuple(claims.get("apps") or ()),
            claims=claims,
        )

    # DRF's user and scoped throttles key on request.user.pk.
    @property
    def pk(self):
        return self.id

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_username(self):
        return self.email or str(self.id)

    def has_app(self, slug):
        return slug in self.apps

    def has_perm(self, perm, obj=None):
        return False

    def has_perms(self, perm_list, obj=None):
        return False

    def has_module_perms(self, app_label):
        return False

    def __str__(self):
        return self.get_username()


def get_local_user(claims):
    """
    The local user record for the token's user, created on first sight with
    an unusable password. Its email follows the token. The claims are kept
    on it as `jivo_claims`.
    """

    User = get_user_model()

    id_field = conf.get("LOCAL_USER_ID_FIELD")
    email_field = User.get_email_field_name()
    email = claims.get("email") or ""

    defaults = {
        "password": make_password(None),
    }

    if email_field != id_field:
        defaults[email_field] = email

    user, created = User.objects.get_or_create(
        **{id_field: str(claims["sub"])},
        defaults=defaults,
    )

    if (
        not created
        and email
        and email_field != id_field
        and getattr(user, email_field) != email
    ):
        setattr(user, email_field, email)
        user.save(update_fields=[email_field])

    user.jivo_claims = claims

    return user
