"""
Signing keys for the tokens this service issues.

Imported by settings, so nothing here may touch models or app registry.
Run ``python -m config.jwt_keys > jwt_private.pem`` to generate a new RSA
private key.
"""

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.exceptions import ImproperlyConfigured
from jwt.algorithms import RSAAlgorithm


def generate_private_key_pem():
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def load_private_key_pem(base_dir):
    """
    The RSA private key from JWT_PRIVATE_KEY_FILE (a path, relative to
    base_dir unless absolute) or JWT_PRIVATE_KEY (the PEM itself; literal
    ``\\n`` sequences are accepted for single-line env files).
    """

    path = os.getenv("JWT_PRIVATE_KEY_FILE")
    pem = os.getenv("JWT_PRIVATE_KEY")

    if path:
        path = Path(path)

        if not path.is_absolute():
            path = Path(base_dir) / path

        try:
            pem = path.read_text()
        except OSError as exc:
            raise ImproperlyConfigured(
                f"JWT_PRIVATE_KEY_FILE: can't read {path}: {exc}"
            ) from exc

    if not pem:
        raise ImproperlyConfigured(
            "JWT_ALGORITHM is RS256, so JWT_PRIVATE_KEY_FILE or "
            "JWT_PRIVATE_KEY must be set. Generate a key with "
            "`python -m config.jwt_keys > jwt_private.pem`."
        )

    pem = pem.replace("\\n", "\n").strip() + "\n"

    try:
        key = serialization.load_pem_private_key(
            pem.encode(),
            password=None,
        )
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            "The JWT private key is not an unencrypted PEM private key."
        ) from exc

    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ImproperlyConfigured(
            "The JWT private key must be an RSA key of at least 2048 bits."
        )

    return pem


def public_key_pem(private_pem):
    key = serialization.load_pem_private_key(
        private_pem.encode(),
        password=None,
    )

    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def public_jwk(public_pem, algorithm):
    """
    The public key as a JWK, with its RFC 7638 thumbprint as ``kid``. Tokens
    carry the same ``kid`` in their header so verifiers can pick the key.
    """

    jwk = RSAAlgorithm.to_jwk(
        serialization.load_pem_public_key(public_pem.encode()),
        as_dict=True,
    )

    # RFC 7517 advises against sending both `key_ops` and `use`.
    jwk.pop("key_ops", None)

    thumbprint_input = json.dumps(
        {name: jwk[name] for name in ("e", "kty", "n")},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    kid = base64.urlsafe_b64encode(
        hashlib.sha256(thumbprint_input).digest()
    ).rstrip(b"=").decode()

    return {
        **jwk,
        "kid": kid,
        "use": "sig",
        "alg": algorithm,
    }


if __name__ == "__main__":
    print(generate_private_key_pem(), end="")
