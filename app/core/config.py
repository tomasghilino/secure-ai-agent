"""Application configuration loaded from environment variables (12-factor style).

Every tunable that affects cost, safety or model choice lives here so it can be changed
per environment without code changes. Model identifiers are deliberately *not*
hard-coded: Bedrock model availability differs per account and region and changes over
time, so the chat model must be set explicitly once it has been verified.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging"]
# fake    -> deterministic, offline, free (unit tests, CI)
# ollama  -> local open-weight models on the developer's GPU (free, private)
# bedrock -> Amazon Bedrock (staging / benchmarks; costs money)
Provider = Literal["fake", "ollama", "bedrock"]

# ElasticMQ from docker compose, as seen from the host (containers override it).
LOCAL_SQS_ENDPOINT = "http://localhost:9324"


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
    llm_provider: Provider = "fake"
    embedding_provider: Provider = "fake"
    # Vector size stored in PostgreSQL. Changing it (or the embedding model) requires
    # re-embedding every chunk: vectors from different models are not comparable.
    embedding_dimensions: int = 1024

    # --- Ollama (local models) ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "qwen3:8b"
    ollama_embedding_model: str = "qwen3-embedding:0.6b"

    # --- AWS / Bedrock (credentials come from the AWS SDK chain, never from this file) ---
    aws_region: str = "us-east-1"
    aws_profile: str | None = None
    bedrock_chat_model_id: str | None = None
    bedrock_embedding_model_id: str = "amazon.titan-embed-text-v2:0"

    # --- Uploads, object storage and queue (ADR-0003) ---
    # Uploaded PDFs: a local directory in development, S3 in staging (Day 4).
    storage_root: Path = Path("data/storage")
    # Unset = real SQS. Only staging may use it: local and test always talk to ElasticMQ,
    # so a development machine never sends messages to AWS by accident.
    sqs_endpoint_url: str | None = None
    ingestion_queue_name: str = "northstar-ingestion"
    # Must match the queue's maxReceiveCount (infra/elasticmq/elasticmq.conf, Terraform):
    # on the last delivery a transient error fails the job instead of leaving it running.
    ingestion_max_receives: int = Field(default=3, ge=1)
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1)

    # --- Cost guardrails ---
    llm_max_output_tokens: int = Field(default=800, ge=64, le=4096)

    @field_validator("embedding_dimensions")
    @classmethod
    def _titan_v2_dimensions(cls, value: int) -> int:
        # Sizes supported by both Titan V2 and Qwen3-Embedding (Matryoshka truncation).
        if value not in (256, 512, 1024):
            raise ValueError("EMBEDDING_DIMENSIONS must be 256, 512 or 1024")
        return value

    @model_validator(mode="after")
    def _require_model_ids_for_bedrock(self) -> Settings:
        if self.llm_provider == "bedrock" and not self.bedrock_chat_model_id:
            raise ValueError(
                "LLM_PROVIDER=bedrock requires BEDROCK_CHAT_MODEL_ID "
                "(verify availability in your region first)."
            )
        return self

    @model_validator(mode="after")
    def _local_queue_endpoint(self) -> Settings:
        if self.app_env != "staging" and self.sqs_endpoint_url is None:
            self.sqs_endpoint_url = LOCAL_SQS_ENDPOINT
        return self


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton; override in tests with `app.dependency_overrides`."""
    return Settings()
