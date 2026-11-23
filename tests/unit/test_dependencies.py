"""Who the API thinks is calling, in both authentication modes (no database)."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api.dependencies import PrincipalDep, require_role
from app.core.config import Settings, get_settings
from app.security.tokens import TokenVerifier
from app.tools.authorization import Principal
from scripts.dev_token import DEV_AUDIENCE, DEV_ISSUER, generate_signing_key, mint, public_jwks

KEY = generate_signing_key()
SUPPORT_HEADERS = {
    "X-Tenant-ID": "nsi-amer",
    "X-User-ID": "agent-1",
    "X-User-Role": "support_agent",
}


def build_app(settings: Settings, verifier: TokenVerifier | None = None) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.token_verifier = verifier

    @app.get("/whoami")
    def whoami(principal: PrincipalDep) -> dict[str, str]:
        return {"tenant": principal.tenant_id, "user": principal.user_id, "role": principal.role}

    return app


async def call(app: FastAPI, headers: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/whoami", headers=headers)


@pytest.fixture
def jwt_settings(tmp_path: Path) -> tuple[Settings, TokenVerifier]:
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(public_jwks(KEY)), encoding="utf-8")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        auth_mode="jwt",
        auth_jwt_issuer=DEV_ISSUER,
        auth_jwt_audience=DEV_AUDIENCE,
        auth_jwks_path=jwks,
    )
    return settings, TokenVerifier.from_settings(settings)


async def test_dev_headers_identify_the_caller_locally() -> None:
    response = await call(build_app(Settings(_env_file=None)), SUPPORT_HEADERS)  # type: ignore[call-arg]

    assert response.json() == {"tenant": "nsi-amer", "user": "agent-1", "role": "support_agent"}


async def test_dev_headers_need_all_three_values() -> None:
    headers = {"X-Tenant-ID": "nsi-amer"}

    response = await call(build_app(Settings(_env_file=None)), headers)  # type: ignore[call-arg]

    assert response.status_code == 401


async def test_a_bearer_token_identifies_the_caller(
    jwt_settings: tuple[Settings, TokenVerifier],
) -> None:
    token = mint(KEY, subject="fin-1", tenant="nsi-emea", roles=["finance_analyst"])

    response = await call(build_app(*jwt_settings), {"Authorization": f"Bearer {token}"})

    assert response.json() == {"tenant": "nsi-emea", "user": "fin-1", "role": "finance_analyst"}


async def test_with_jwt_on_the_identity_headers_are_ignored(
    jwt_settings: tuple[Settings, TokenVerifier],
) -> None:
    token = mint(KEY, subject="agent-1", tenant="nsi-amer", roles=["support_agent"])
    spoofed = {"X-Tenant-ID": "nsi-emea", "X-User-ID": "boss", "X-User-Role": "supervisor"}

    no_token = await call(build_app(*jwt_settings), spoofed)
    with_token = await call(
        build_app(*jwt_settings), {**spoofed, "Authorization": f"Bearer {token}"}
    )

    assert no_token.status_code == 401
    assert with_token.json() == {"tenant": "nsi-amer", "user": "agent-1", "role": "support_agent"}


async def test_a_rejected_token_gets_a_generic_401_with_a_challenge(
    jwt_settings: tuple[Settings, TokenVerifier],
) -> None:
    token = mint(KEY, subject="u-1", tenant="nsi-amer", roles=["support_agent"], audience="x")

    response = await call(build_app(*jwt_settings), {"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid or expired token"}  # no hint for forgers
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


async def test_a_token_without_a_role_is_forbidden(
    jwt_settings: tuple[Settings, TokenVerifier],
) -> None:
    token = mint(KEY, subject="u-1", tenant="nsi-amer", roles=[])

    response = await call(build_app(*jwt_settings), {"Authorization": f"Bearer {token}"})

    assert response.status_code == 403


def test_require_role_refuses_other_roles() -> None:
    check = require_role({"editor"}, "manage documents")

    assert check(Principal("nsi-amer", "ed-1", "editor")).user_id == "ed-1"
    with pytest.raises(HTTPException) as error:
        check(Principal("nsi-amer", "sup-1", "supervisor"))
    assert error.value.status_code == 403
