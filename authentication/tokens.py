import jwt
from django.conf import settings
from rest_framework_simplejwt.backends import TokenBackend
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from applications.models import app_slugs


class KeyIdTokenBackend(TokenBackend):
    """
    SimpleJWT's TokenBackend, plus the signing key's ID in the `kid` header
    so verifiers using /.well-known/jwks.json can pick the right key.
    """

    def __init__(self, *args, key_id=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.key_id = key_id

    def encode(self, payload):
        jwt_payload = payload.copy()

        if self.audience is not None:
            jwt_payload["aud"] = self.audience

        if self.issuer is not None:
            jwt_payload["iss"] = self.issuer

        return jwt.encode(
            jwt_payload,
            self.prepared_signing_key,
            algorithm=self.algorithm,
            headers={"kid": self.key_id} if self.key_id else None,
            json_encoder=self.json_encoder,
        )


def install_token_backend():
    """
    Make every SimpleJWT token use KeyIdTokenBackend. SimpleJWT has no
    setting for the backend class; tokens look up
    `rest_framework_simplejwt.state.token_backend` when first used, so
    replacing it at startup covers issuing and verifying alike.
    """

    from rest_framework_simplejwt import state
    from rest_framework_simplejwt.settings import api_settings

    jwk = settings.JWT_PUBLIC_JWK

    state.token_backend = KeyIdTokenBackend(
        api_settings.ALGORITHM,
        api_settings.SIGNING_KEY,
        api_settings.VERIFYING_KEY,
        api_settings.AUDIENCE,
        api_settings.ISSUER,
        api_settings.JWK_URL,
        api_settings.LEEWAY,
        api_settings.JSON_ENCODER,
        key_id=jwk["kid"] if jwk else None,
    )


def add_user_claims(token, user):
    """
    Claims other applications read without calling this service. Access
    tokens made from a refresh token copy them.
    """

    token["email"] = user.email
    token["apps"] = app_slugs(user)

    return token


def issue_tokens(user):
    """A new refresh token (and, through it, access token) for the user."""

    refresh = add_user_claims(
        RefreshToken.for_user(user),
        user,
    )

    # for_user() recorded the token before the claims were added; store the
    # token as issued, so a replay within the grace period gets it back.
    OutstandingToken.objects.filter(
        jti=refresh["jti"],
    ).update(
        token=str(refresh),
    )

    return refresh
