"""Application configuration loaded from environment variables (12-factor style).

Every tunable that affects cost, safety or model choice lives here so it can be changed
per environment without code changes. Model identifiers are deliberately *not*
hard-coded: Bedrock model availability differs per account and region and changes over
time, so the chat model must be set explicitly once it has been verified.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging"]
Provider = Literal["fake", "bedrock"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `FOO=` in .env means "use the default", not "empty string"
        extra="ignore",
    )

    # --- Application ---
    app_env: Environment = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"

    # --- Database ---
    database_url: str = "postgresql+psycopg://northstar:northstar@localhost:5432/northstar"

    # --- Model providers ---
    # `fake` providers are deterministic and free: used by tests, CI and offline development.
    llm_provider: Provider = "fake"
    embedding_provider: Provider = "fake"

    # --- AWS / Bedrock (credentials come from the AWS SDK chain, never from this file) ---
    aws_region: str = "us-east-1"
    aws_profile: str | None = None
    bedrock_chat_model_id: str | None = None
    bedrock_embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    embedding_dimensions: int = 1024  # Titan V2 supports 256 | 512 | 1024

    # --- Cost guardrails ---
    llm_max_output_tokens: int = Field(default=800, ge=64, le=4096)

    @field_validator("embedding_dimensions")
    @classmethod
    def _titan_v2_dimensions(cls, value: int) -> int:
        if value not in (256, 512, 1024):
            raise ValueError("EMBEDDING_DIMENSIONS must be 256, 512 or 1024 (Titan V2)")
        return value

    @model_validator(mode="after")
    def _require_model_ids_for_bedrock(self) -> Settings:
        if self.llm_provider == "bedrock" and not self.bedrock_chat_model_id:
            raise ValueError(
                "LLM_PROVIDER=bedrock requires BEDROCK_CHAT_MODEL_ID "
                "(verify availability in your region first)."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton; override in tests with `app.dependency_overrides`."""
    return Settings()
