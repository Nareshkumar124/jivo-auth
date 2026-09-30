import uuid

import jwt
from django.core.exceptions import ImproperlyConfigured

from . import settings as conf
from .exceptions import InvalidToken
from .keys import get_jwks_cache


def _verifying_key(token):
    algorithm = conf.algorithm()

    if algorithm.startswith("HS"):
        secret = conf.get("SECRET")

        if not secret:
            raise ImproperlyConfigured(
                f"JIVO_AUTH['SECRET'] is required for {algorithm}."
            )

        return secret

    public_key = conf.get("PUBLIC_KEY")

    if public_key:
        # Env files often hold the PEM on one line with literal "\n"s.
        return public_key.replace("\\n", "\n")

    url = conf.jwks_url()

    if not url:
        raise ImproperlyConfigured(
            "Set JIVO_AUTH['URL'] (or JIVO_AUTH['PUBLIC_KEY']) so tokens "
            "can be verified."
        )

    kid = jwt.get_unverified_header(token).get("kid")

    if not kid:
        raise InvalidToken("Invalid token.")

    key = get_jwks_cache(url, conf.get("TIMEOUT")).get(kid)

    if key is None:
        raise InvalidToken("Invalid token.")

    return key.key


def decode_access_token(token):
    """
    Verify a Jivo Auth access token (signature, expiry, issuer, type) and
    return its claims. Raises InvalidToken.
    """

    issuer = conf.issuer()

    try:
        claims = jwt.decode(
            token,
            _verifying_key(token),
            algorithms=[conf.algorithm()],
            issuer=issuer,
            leeway=conf.get("LEEWAY"),
            options={
                "require": ["exp", "iat", "sub"] + (["iss"] if issuer else []),
            },
        )

    except jwt.ExpiredSignatureError as exc:
        raise InvalidToken("Token has expired.") from exc

    except jwt.PyJWTError as exc:
        raise InvalidToken("Invalid token.") from exc

    if claims.get("token_type") != "access":
        raise InvalidToken("Access token required.")

    try:
        uuid.UUID(str(claims["sub"]))
    except ValueError as exc:
        raise InvalidToken("Invalid token.") from exc

    return claims


def has_app_access(claims):
    """
    Whether the token's `apps` claim lists JIVO_AUTH['APP']. Without APP,
    only if ALLOW_ALL_USERS says so: a forgotten setting must not let every
    Jivo Auth user in.
    """

    app = conf.get("APP")

    if app:
        return app in (claims.get("apps") or [])

    return bool(conf.get("ALLOW_ALL_USERS"))
