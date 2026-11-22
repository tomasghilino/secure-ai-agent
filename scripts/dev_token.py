"""Local token issuer for trying JWT authentication without an identity provider.

    make token role=supervisor user=sup-1          # prints a bearer token (1 hour)

The first run creates an RSA signing key and its public JWKS in ``.auth/`` (git-ignored).
Point the API at it with:

    AUTH_MODE=jwt
    AUTH_JWT_ISSUER=https://dev-issuer.local
    AUTH_JWT_AUDIENCE=secure-ai-agent
    AUTH_JWKS_PATH=.auth/jwks.json

In a deployed environment the provider (e.g. Amazon Cognito) holds the private key and
the API only ever sees its public JWKS URL; nothing here is used there.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.tools.authorization import ROLES

AUTH_DIR = Path(__file__).resolve().parents[1] / ".auth"
DEV_ISSUER = "https://dev-issuer.local"
DEV_AUDIENCE = "secure-ai-agent"
ALGORITHM = "RS256"


def generate_signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def key_id(private_key: rsa.RSAPrivateKey) -> str:
    """RFC 7638 thumbprint: a stable kid derived from the public key itself."""
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    canonical = json.dumps({k: jwk[k] for k in ("e", "kty", "n")}, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def public_jwks(*private_keys: rsa.RSAPrivateKey) -> dict[str, Any]:
    keys = []
    for private_key in private_keys:
        jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
        keys.append({**jwk, "kid": key_id(private_key), "use": "sig", "alg": ALGORITHM})
    return {"keys": keys}


def mint(
    private_key: rsa.RSAPrivateKey,
    *,
    subject: str,
    tenant: str | None,
    roles: list[str] | str | None,
    issuer: str = DEV_ISSUER,
    audience: str = DEV_AUDIENCE,
    ttl: timedelta = timedelta(hours=1),
    now: datetime | None = None,
    extra: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
) -> str:
    issued = now or datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "iat": issued,
        "nbf": issued,
        "exp": issued + ttl,
        "jti": str(uuid.uuid4()),
    }
    if tenant is not None:
        claims["tenant_id"] = tenant
    if roles is not None:
        claims["roles"] = roles
    claims.update(extra or {})
    return jwt.encode(
        claims,
        private_key,
        algorithm=ALGORITHM,
        headers={"kid": key_id(private_key), **(headers or {})},
    )


def load_or_create_key(directory: Path = AUTH_DIR) -> rsa.RSAPrivateKey:
    key_file, jwks_file = directory / "dev-signing-key.pem", directory / "jwks.json"
    if key_file.exists():
        loaded = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise TypeError(f"{key_file} is not an RSA key")
        return loaded
    directory.mkdir(exist_ok=True)
    private_key = generate_signing_key()
    key_file.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    jwks_file.write_text(json.dumps(public_jwks(private_key), indent=2), encoding="utf-8")
    return private_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint a development bearer token")
    parser.add_argument("--tenant", default="nsi-amer")
    parser.add_argument("--role", default="support_agent", choices=ROLES)
    parser.add_argument("--user", default="agent-1")
    parser.add_argument("--minutes", type=int, default=60)
    args = parser.parse_args()
    token = mint(
        load_or_create_key(),
        subject=args.user,
        tenant=args.tenant,
        roles=[args.role],
        ttl=timedelta(minutes=args.minutes),
    )
    print(token)


if __name__ == "__main__":
    main()
