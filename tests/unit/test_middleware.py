"""Security headers, request IDs and error responses that reveal nothing (no database)."""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from app import main
from app.api.middleware import SecurityMiddleware, validation_error_handler
from app.core.config import Settings


class Secret(BaseModel):
    api_key: str = Field(pattern=r"^ak_[a-z0-9]{8}$")


def build_app(*, hsts: bool = False) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityMiddleware, hsts=hsts)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("connection string postgresql://user:hunter2@db/prod")

    @app.post("/secret")
    async def secret(body: Secret) -> None:
        return None

    return app


def client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_every_response_carries_the_security_headers() -> None:
    async with client(build_app()) as http:
        response = await http.get("/ok")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert "Strict-Transport-Security" not in response.headers  # only behind TLS


async def test_hsts_is_sent_where_tls_terminates() -> None:
    async with client(build_app(hsts=True)) as http:
        response = await http.get("/ok")

    assert response.headers["Strict-Transport-Security"].startswith("max-age=")


async def test_a_crash_becomes_a_generic_500_with_the_request_id() -> None:
    async with client(build_app()) as http:
        response = await http.get("/boom", headers={"X-Request-ID": "req-12345678"})

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error", "request_id": "req-12345678"}
    assert "hunter2" not in response.text
    assert response.headers["X-Content-Type-Options"] == "nosniff"  # headers on errors too


async def test_an_unsafe_request_id_is_replaced() -> None:
    async with client(build_app()) as http:
        response = await http.get("/ok", headers={"X-Request-ID": "<script>alert(1)</script>"})

    assert response.headers["X-Request-ID"] != "<script>alert(1)</script>"
    assert len(response.headers["X-Request-ID"]) == 32


async def test_validation_errors_do_not_echo_the_submitted_value() -> None:
    async with client(build_app()) as http:
        response = await http.post("/secret", json={"api_key": "sk_live_DO_NOT_LEAK"})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "api_key"]
    assert "DO_NOT_LEAK" not in response.text


async def test_the_api_schema_is_not_served_in_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    staging = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="staging",
        auth_mode="jwt",
        auth_jwt_issuer="https://issuer.example",
        auth_jwt_audience="secure-ai-agent",
        auth_jwks_url="https://issuer.example/jwks.json",
    )
    monkeypatch.setattr(main, "get_settings", lambda: staging)

    async with client(main.create_app()) as http:
        docs = await http.get("/docs")
        schema = await http.get("/openapi.json")

    assert (docs.status_code, schema.status_code) == (404, 404)
