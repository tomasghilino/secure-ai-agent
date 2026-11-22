import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import Settings


def test_defaults_use_free_fake_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.llm_provider == "fake"
    assert settings.embedding_provider == "fake"


def test_bedrock_llm_requires_explicit_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    monkeypatch.delenv("BEDROCK_CHAT_MODEL_ID", raising=False)

    with pytest.raises(ValidationError, match="BEDROCK_CHAT_MODEL_ID"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_embedding_dimensions_parsed_from_env_and_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "512")
    assert Settings(_env_file=None).embedding_dimensions == 512  # type: ignore[call-arg]

    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "768")
    with pytest.raises(ValidationError, match="256, 512 or 1024"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_empty_env_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_PROFILE", "")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.aws_profile is None


@pytest.mark.parametrize("app_env", ["local", "test"])
def test_local_environments_always_use_the_local_queue(
    monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.delenv("SQS_ENDPOINT_URL", raising=False)

    assert Settings(_env_file=None).sqs_endpoint_url == "http://localhost:9324"  # type: ignore[call-arg]


# What a deployed environment needs: verified tokens instead of identity headers.
STAGING_AUTH = {
    "AUTH_MODE": "jwt",
    "AUTH_JWT_ISSUER": "https://issuer.example",
    "AUTH_JWT_AUDIENCE": "secure-ai-agent",
    "AUTH_JWKS_URL": "https://issuer.example/.well-known/jwks.json",
}


def test_staging_uses_the_real_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    for name, value in STAGING_AUTH.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SQS_ENDPOINT_URL", raising=False)

    assert Settings(_env_file=None).sqs_endpoint_url is None  # type: ignore[call-arg]


def test_staging_refuses_to_start_with_identity_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(ValidationError, match="dev_headers is only allowed"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"AUTH_JWT_AUDIENCE": ""}, "AUTH_JWT_ISSUER and AUTH_JWT_AUDIENCE"),
        ({"AUTH_JWKS_PATH": "jwks.json"}, "exactly one of"),
        ({"AUTH_JWKS_URL": "http://issuer.example/jwks.json"}, "must use https"),
        ({"AUTH_JWT_ALGORITHMS": '["HS256"]'}, "must be asymmetric"),
        ({"AUTH_JWT_ALGORITHMS": '["none"]'}, "must be asymmetric"),
    ],
)
def test_an_incomplete_jwt_setup_is_refused(
    monkeypatch: pytest.MonkeyPatch, override: dict[str, str], message: str
) -> None:
    for name, value in {**STAGING_AUTH, **override}.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_the_jwks_can_be_given_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in STAGING_AUTH.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AUTH_JWKS_URL", raising=False)
    monkeypatch.setenv("AUTH_JWKS_JSON", '{"keys": []}')

    assert Settings(_env_file=None).auth_jwks_json == '{"keys": []}'  # type: ignore[call-arg]


def test_the_database_password_is_injected_into_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://app@db.internal:5432/northstar")
    monkeypatch.setenv("DATABASE_PASSWORD", "p@ss:w/rd?#")  # characters a URL must escape

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    url = make_url(settings.database_url)
    assert (url.username, url.password, url.host) == ("app", "p@ss:w/rd?#", "db.internal")
    assert "p@ss:w/rd?#" not in repr(settings)  # a SecretStr never prints its value


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"STORAGE_BACKEND": "s3", "STORAGE_BUCKET": "docs"}, "only allowed in staging"),
        ({"STORAGE_BACKEND": "s3", "APP_ENV": "staging"}, "requires STORAGE_BUCKET"),
    ],
)
def test_s3_storage_is_only_used_in_staging_with_a_bucket(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    for name, value in {**STAGING_AUTH, "APP_ENV": "local", **env}.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None)  # type: ignore[call-arg]
