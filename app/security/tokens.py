"""Bearer token verification: a signed JWT from an OpenID Connect provider -> ``Principal``.

What is checked, in order (any failure is a 401 with the same generic message):

1. The header's ``alg`` is in the configured allowlist, which only holds asymmetric
   algorithms (``Settings`` rejects ``none`` and HMAC), so a public key can never be used
   as an HMAC secret (algorithm confusion).
2. The signing key is found by ``kid`` in the provider's JWKS (a URL, a local file in
   development, or the document itself). An unknown ``kid`` is rejected, never "tried with
   every key".
3. Signature, ``exp``, ``nbf``, ``iat``, exact ``iss`` and audience, with a small clock
   skew. The audience is the standard ``aud`` claim, or, for Amazon Cognito access tokens
   (which carry none), ``client_id`` together with ``token_use=access``: an ID token of
   the same client is refused.
4. The identity claims: ``sub`` (the user), a tenant claim and exactly one known role.
   A valid token without a tenant or a role is a 403: authenticated, not authorized.

The verified identity is the only one the API uses: tenant, user and role are never read
from a request body, a header or a tool argument once JWT authentication is on.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

import jwt

from app.core.config import Settings
from app.tools.authorization import Principal, parse_role

TENANT_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")
# Provider subjects: UUIDs (Cognito), "auth0|abc", e-mail-like ids. Stored in audit rows.
SUBJECT_PATTERN = re.compile(r"^[A-Za-z0-9._@:|+-]{1,128}$")
REQUIRED_CLAIMS = ["exp", "iat", "iss", "sub"]


class InvalidToken(Exception):
    """Not authenticated (401): missing, malformed, expired or not signed by our issuer."""


class AccessDenied(Exception):
    """Authenticated but not authorized (403): no tenant or no single known role."""


class SigningKeys(Protocol):
    def key_for(self, kid: str | None) -> Any: ...


class JwksDocument:
    """Public keys from a JWKS document held in memory: a local file in development and
    tests, or the provider's keys delivered with the configuration."""

    def __init__(self, document: str) -> None:
        self._keys = jwt.PyJWKSet.from_json(document)

    @classmethod
    def from_file(cls, path: Path) -> JwksDocument:
        return cls(path.read_text(encoding="utf-8"))

    def key_for(self, kid: str | None) -> Any:
        keys = self._keys.keys
        if kid is None:
            if len(keys) != 1:
                raise InvalidToken("token has no kid")
            return keys[0].key
        for key in keys:
            if key.key_id == kid:
                return key.key
        raise InvalidToken("unknown signing key")


class JwksEndpoint:
    """Public keys from the provider's JWKS URL, cached; refetched when a new ``kid``
    appears (key rotation)."""

    def __init__(self, url: str) -> None:
        self._client = jwt.PyJWKClient(url, cache_keys=True, lifespan=300, timeout=5)

    def key_for(self, kid: str | None) -> Any:
        if kid is None:
            raise InvalidToken("token has no kid")
        try:
            return self._client.get_signing_key(kid).key
        except jwt.PyJWKClientError as exc:
            raise InvalidToken("unknown signing key") from exc


class TokenVerifier:
    def __init__(
        self,
        keys: SigningKeys,
        *,
        issuer: str,
        audience: str,
        algorithms: Sequence[str] = ("RS256",),
        audience_claim: Literal["aud", "client_id"] = "aud",
        tenant_claim: str = "tenant_id",
        roles_claim: str = "roles",
        leeway_seconds: int = 30,
    ) -> None:
        self._keys = keys
        self._issuer = issuer
        self._audience = audience
        self._algorithms = list(algorithms)
        self._audience_claim = audience_claim
        self._tenant_claim = tenant_claim
        self._roles_claim = roles_claim
        self._leeway = leeway_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> TokenVerifier:
        if settings.auth_jwt_issuer is None or settings.auth_jwt_audience is None:
            raise ValueError("JWT authentication is not configured")  # Settings rejects it too
        keys: SigningKeys
        if settings.auth_jwks_url:
            keys = JwksEndpoint(settings.auth_jwks_url)
        elif settings.auth_jwks_path:
            keys = JwksDocument.from_file(settings.auth_jwks_path)
        else:
            keys = JwksDocument(settings.auth_jwks_json or "")  # Settings requires one
        return cls(
            keys,
            issuer=settings.auth_jwt_issuer,
            audience=settings.auth_jwt_audience,
            algorithms=settings.auth_jwt_algorithms,
            audience_claim=settings.auth_audience_claim,
            tenant_claim=settings.auth_tenant_claim,
            roles_claim=settings.auth_roles_claim,
            leeway_seconds=settings.auth_clock_skew_seconds,
        )

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidToken("malformed token") from exc
        if header.get("alg") not in self._algorithms:
            raise InvalidToken(f"algorithm {header.get('alg')!r} is not accepted")
        key = self._keys.key_for(header.get("kid"))
        standard = self._audience_claim == "aud"
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=self._algorithms,
                audience=self._audience if standard else None,
                issuer=self._issuer,
                leeway=self._leeway,
                options={
                    "require": [*REQUIRED_CLAIMS, "aud" if standard else "client_id"],
                    "verify_aud": standard,
                },
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(f"{type(exc).__name__}") from exc
        if not standard and (
            claims.get("client_id") != self._audience or claims.get("token_use") != "access"
        ):
            raise InvalidToken("not an access token for this client")
        return self._principal(claims)

    def _principal(self, claims: dict[str, Any]) -> Principal:
        subject = claims.get("sub")
        if not isinstance(subject, str) or not SUBJECT_PATTERN.match(subject):
            raise InvalidToken("invalid subject")
        tenant = claims.get(self._tenant_claim)
        if not isinstance(tenant, str) or not TENANT_PATTERN.match(tenant):
            raise AccessDenied("the token carries no valid tenant")
        raw = claims.get(self._roles_claim)
        values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
        roles = {role for value in values if (role := parse_role(str(value))) is not None}
        # One identity, one role: a token with several application roles is ambiguous,
        # and picking "the strongest" would be a silent privilege decision.
        if len(roles) != 1:
            raise AccessDenied("the token must carry exactly one application role")
        return Principal(tenant, subject, roles.pop())
