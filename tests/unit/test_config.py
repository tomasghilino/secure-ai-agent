import pytest
from pydantic import ValidationError

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
