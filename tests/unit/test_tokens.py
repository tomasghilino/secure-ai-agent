"""Bearer token verification: each check a forged or misused token must fail (no network)."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from app.security.tokens import AccessDenied, InvalidToken, JwksDocument, TokenVerifier
from app.tools.authorization import Principal
from scripts.dev_token import DEV_AUDIENCE, DEV_ISSUER, generate_signing_key, mint, public_jwks

KEY = generate_signing_key()
OTHER_KEY = generate_signing_key()  # not in our JWKS: an attacker's key


@pytest.fixture(scope="module")
def verifier(tmp_path_factory: pytest.TempPathFactory) -> TokenVerifier:
    jwks = tmp_path_factory.mktemp("auth") / "jwks.json"
    jwks.write_text(json.dumps(public_jwks(KEY)), encoding="utf-8")
    return TokenVerifier(JwksDocument.from_file(jwks), issuer=DEV_ISSUER, audience=DEV_AUDIENCE)


def token(**overrides: object) -> str:
    claims: dict[str, object] = {
        "subject": "sup-1",
        "tenant": "nsi-amer",
        "roles": ["supervisor"],
    }
    claims.update(overrides)
    return mint(KEY, **claims)  # type: ignore[arg-type]


def b64(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_a_valid_token_becomes_the_principal(verifier: TokenVerifier) -> None:
    assert verifier.verify(token()) == Principal("nsi-amer", "sup-1", "supervisor")


def test_roles_may_be_a_single_string(verifier: TokenVerifier) -> None:
    assert verifier.verify(token(roles="finance_analyst")).role == "finance_analyst"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"issuer": "https://evil.example"}, id="wrong issuer"),
        pytest.param({"audience": "another-api"}, id="wrong audience"),
        pytest.param(
            {"now": datetime.now(UTC) - timedelta(hours=2), "ttl": timedelta(hours=1)},
            id="expired",
        ),
        pytest.param({"now": datetime.now(UTC) + timedelta(hours=1)}, id="not yet valid"),
        pytest.param({"subject": "x" * 200}, id="oversized subject"),
    ],
)
def test_claims_are_checked(verifier: TokenVerifier, overrides: dict[str, object]) -> None:
    with pytest.raises(InvalidToken):
        verifier.verify(token(**overrides))


def test_a_token_signed_by_another_key_is_refused(verifier: TokenVerifier) -> None:
    # Same kid as our key, signature from the attacker's key.
    forged = mint(OTHER_KEY, subject="sup-1", tenant="nsi-amer", roles=["supervisor"])
    header, payload, _ = forged.split(".")
    ours = token().split(".")[0]

    with pytest.raises(InvalidToken):
        verifier.verify(forged)
    with pytest.raises(InvalidToken):
        verifier.verify(f"{ours}.{payload}.{forged.split('.')[2]}")
    assert header != ours  # different kid


def test_an_unknown_kid_is_refused(verifier: TokenVerifier) -> None:
    with pytest.raises(InvalidToken, match="unknown signing key"):
        verifier.verify(token(headers={"kid": "someone-elses-key"}))


def test_a_tampered_payload_is_refused(verifier: TokenVerifier) -> None:
    header, payload, signature = token(roles=["support_agent"]).split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["roles"] = ["supervisor"]  # privilege escalation attempt

    with pytest.raises(InvalidToken):
        verifier.verify(f"{header}.{b64(claims)}.{signature}")


def test_alg_none_is_refused(verifier: TokenVerifier) -> None:
    payload = token().split(".")[1]
    unsigned = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{payload}."

    with pytest.raises(InvalidToken, match="not accepted"):
        verifier.verify(unsigned)


def test_hmac_with_the_public_key_is_refused(verifier: TokenVerifier) -> None:
    # Algorithm confusion: an HS256 signature made with our *public* key as the secret.
    public_pem = KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header = b64({"alg": "HS256", "typ": "JWT", "kid": token_kid()})
    body = token().split(".")[1]
    digest = hmac.new(public_pem, f"{header}.{body}".encode(), hashlib.sha256).digest()
    forged = f"{header}.{body}.{base64.urlsafe_b64encode(digest).rstrip(b'=').decode()}"

    with pytest.raises(InvalidToken, match="not accepted"):
        verifier.verify(forged)


def test_garbage_is_refused(verifier: TokenVerifier) -> None:
    with pytest.raises(InvalidToken):
        verifier.verify("not-a-token")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"tenant": None}, id="no tenant"),
        pytest.param({"tenant": "NSI AMER; DROP"}, id="malformed tenant"),
        pytest.param({"roles": None}, id="no role"),
        pytest.param({"roles": ["admin"]}, id="unknown role"),
        pytest.param({"roles": ["support_agent", "supervisor"]}, id="ambiguous roles"),
    ],
)
def test_a_valid_token_without_tenant_or_single_role_is_forbidden(
    verifier: TokenVerifier, overrides: dict[str, object]
) -> None:
    with pytest.raises(AccessDenied):
        verifier.verify(token(**overrides))


def test_required_claims_must_be_present(verifier: TokenVerifier) -> None:
    claims = {"iss": DEV_ISSUER, "aud": DEV_AUDIENCE, "sub": "u", "tenant_id": "nsi-amer"}
    no_expiry = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": token_kid()})

    with pytest.raises(InvalidToken):
        verifier.verify(no_expiry)


def token_kid() -> str:
    return str(jwt.get_unverified_header(token())["kid"])


# --------------------------------------------------------------------------- Cognito access tokens

COGNITO_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Example"
CLIENT_ID = "4example0client0id"


@pytest.fixture(scope="module")
def cognito_verifier() -> TokenVerifier:
    return TokenVerifier(
        JwksDocument(json.dumps(public_jwks(KEY))),
        issuer=COGNITO_ISSUER,
        audience=CLIENT_ID,
        audience_claim="client_id",
        tenant_claim="tenant_id",
        roles_claim="cognito:groups",
    )


def cognito_token(**claims: object) -> str:
    """The shape of a Cognito access token: no `aud`, the app client in `client_id`."""
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "iss": COGNITO_ISSUER,
        "sub": "3f1c6a52-0000-4000-8000-000000000001",
        "client_id": CLIENT_ID,
        "token_use": "access",
        "tenant_id": "nsi-amer",
        "cognito:groups": ["supervisor"],
        "iat": now,
        "exp": now + timedelta(minutes=30),
    }
    payload.update(claims)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, KEY, algorithm="RS256", headers={"kid": token_kid()})


def test_a_cognito_access_token_becomes_the_principal(cognito_verifier: TokenVerifier) -> None:
    principal = cognito_verifier.verify(cognito_token())

    assert principal == Principal("nsi-amer", "3f1c6a52-0000-4000-8000-000000000001", "supervisor")


@pytest.mark.parametrize(
    "claims",
    [
        {"token_use": "id"},  # an ID token of the same client is not an API credential
        {"token_use": None},
        {"client_id": "another-app-client"},
        {"client_id": None},
        {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Other"},
    ],
)
def test_cognito_tokens_for_another_client_or_use_are_refused(
    cognito_verifier: TokenVerifier, claims: dict[str, object]
) -> None:
    with pytest.raises(InvalidToken):
        cognito_verifier.verify(cognito_token(**claims))


def test_the_standard_mode_still_requires_aud(verifier: TokenVerifier) -> None:
    with pytest.raises(InvalidToken):
        verifier.verify(cognito_token(iss=DEV_ISSUER, client_id=DEV_AUDIENCE))
