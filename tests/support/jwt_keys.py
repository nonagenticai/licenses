"""RSA keypair + issuer used to sign JWTs in tests.

The suite previously handed the middleware unsigned tokens
(``header.payload.mock_signature``) because the middleware did not verify
signatures. Now that it does, tests must present genuinely signed tokens.

The keypair is generated once per process and never leaves it. Tests point
``auth.jwt_verify._signing_key`` at :func:`public_key` so no JWKS fetch (and
no network) is involved.
"""

from functools import lru_cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Matches the issuer the existing KeycloakUser fixtures already declare.
TEST_ISSUER = "https://keycloak.example.com/realms/secure-apps"


@lru_cache(maxsize=1)
def _keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def private_key_pem() -> bytes:
    return _keypair().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key() -> rsa.RSAPublicKey:
    return _keypair().public_key()


def public_key_pem() -> bytes:
    return public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
