import logging
import uuid
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.db.models import Q

from . import settings as conf


logger = logging.getLogger(__name__)


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
    The local user record for the token's user, found by
    JIVO_AUTH['LOCAL_USER_ID_FIELD'] and created on first sight with an
    unusable password. With LOCAL_USER_LINK_BY_EMAIL, an existing record
    without a Jivo ID and with the token's (verified) email is linked
    instead of creating a second one. Its email follows the token. The
    claims are kept on it as `jivo_claims`.
    """

    User = get_user_model()

    id_field = conf.get("LOCAL_USER_ID_FIELD")
    email_field = User.get_email_field_name()
    jivo_id = str(claims["sub"])
    email = claims.get("email") or ""

    user = User.objects.filter(**{id_field: jivo_id}).first()

    if user is None and email and conf.get("LOCAL_USER_LINK_BY_EMAIL"):
        user = link_by_email(User, id_field, email_field, jivo_id, email)

    created = False

    if user is None:
        user, created = User.objects.get_or_create(
            **{id_field: jivo_id},
            defaults=new_user_fields(User, id_field, email_field, jivo_id, email),
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


def new_user_fields(User, id_field, email_field, jivo_id, email):
    defaults = {
        "password": make_password(None),
    }

    if email_field != id_field:
        defaults[email_field] = email

    # A model that keeps its own username (unique, and no longer used to
    # log in) gets the email, or the Jivo ID when that's taken or too long.
    username_field = User.USERNAME_FIELD

    if username_field not in (id_field, email_field):
        max_length = User._meta.get_field(username_field).max_length
        fits = email and (max_length is None or len(email) <= max_length)
        taken = fits and User.objects.filter(**{username_field: email}).exists()

        defaults[username_field] = email if fits and not taken else jivo_id

    return defaults


def link_by_email(User, id_field, email_field, jivo_id, email):
    """
    Give the one local record with this email and no Jivo ID the Jivo ID,
    or return None. Records that already have a Jivo ID are never taken.
    """

    unlinked = Q(**{f"{id_field}__isnull": True})

    if User._meta.get_field(id_field).empty_strings_allowed:
        unlinked |= Q(**{id_field: ""})

    candidates = list(
        User.objects.filter(unlinked, **{f"{email_field}__iexact": email})[:2]
    )

    if len(candidates) != 1:
        if candidates:
            logger.warning(
                "Not linking Jivo user %s by email: several local users "
                "have %s. Set %s on the right one.",
                jivo_id,
                email,
                id_field,
            )
        return None

    user = candidates[0]

    # Only if still unlinked: a concurrent request may have linked it.
    claimed = User.objects.filter(unlinked, pk=user.pk).update(**{id_field: jivo_id})

    if not claimed:
        return User.objects.filter(**{id_field: jivo_id}).first()

    logger.info("Linked local user %s to Jivo user %s by email.", user.pk, jivo_id)

    return User.objects.get(pk=user.pk)
