"""Application configuration loaded from environment variables (12-factor style).

Every tunable that affects cost, safety or model choice lives here so it can be changed
per environment without code changes. Model identifiers are deliberately *not*
hard-coded: Bedrock model availability differs per account and region and changes over
time, so the chat model must be set explicitly once it has been verified.
"""

from __future__ import annotations

from datetime import date
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

# dev_headers -> identity from X-Tenant-ID / X-User-ID / X-User-Role (local and test only)
# jwt         -> a signed bearer token from an OpenID Connect provider (required in staging)
AuthMode = Literal["dev_headers", "jwt"]
# Asymmetric signatures only. "none" and HMAC (HS256...) are never accepted, so a public
# key can never be replayed as an HMAC secret (algorithm confusion).
ASYMMETRIC_JWT_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)


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

    # --- Authentication (ADR-0009) ---
    auth_mode: AuthMode = "dev_headers"
    auth_jwt_issuer: str | None = None  # exact `iss` expected
    auth_jwt_audience: str | None = None  # exact `aud` expected (this API)
    # Where the provider's public keys come from: its JWKS endpoint in deployed
    # environments, or a local file in development (`make token` creates one).
    auth_jwks_url: str | None = None
    auth_jwks_path: Path | None = None
    auth_jwt_algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    # Claim names differ per provider (Cognito: "custom:tenant_id", "cognito:groups").
    auth_tenant_claim: str = "tenant_id"
    auth_roles_claim: str = "roles"
    auth_clock_skew_seconds: int = Field(default=30, ge=0, le=300)

    # --- Abuse and cost limits (ADR-0009) ---
    # Requests per user per minute on every /v1 endpoint.
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10_000)
    # Model tokens (input + output) a tenant may spend per UTC day: "denial of wallet".
    tenant_daily_token_budget: int = Field(default=500_000, ge=1_000)

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
    # Uploaded PDFs: a local directory in development, S3 in staging (planned).
    storage_root: Path = Path("data/storage")
    # Unset = real SQS. Only staging may use it: local and test always talk to ElasticMQ,
    # so a development machine never sends messages to AWS by accident.
    sqs_endpoint_url: str | None = None
    ingestion_queue_name: str = "northstar-ingestion"
    # Must match the queue's maxReceiveCount (infra/elasticmq/elasticmq.conf, Terraform):
    # on the last delivery a transient error fails the job instead of leaving it running.
    ingestion_max_receives: int = Field(default=3, ge=1)
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1)

    # --- RAG ---
    # dense | keyword | hybrid. Compare them with `make eval-retrieval` before changing it.
    rag_retrieval_mode: Literal["dense", "keyword", "hybrid"] = "hybrid"
    rag_top_k: int = Field(default=8, ge=1, le=20)  # chunks retrieved per question
    # Prompt budget for the sources: more context costs more and buries the best passage.
    rag_max_context_tokens: int = Field(default=3000, ge=200, le=12000)

    # Let the local model "think" before answering (Qwen3). Measured on the evaluation set:
    # fixes table lookups the plain mode gets wrong, at ~10x latency and tokens.
    llm_reasoning: bool = False

    # --- Cost guardrails ---
    llm_max_output_tokens: int = Field(default=800, ge=64, le=4096)

    # --- Agent ---
    # Kill switch: false makes every agent endpoint answer 503 without calling the model.
    agent_enabled: bool = True
    # Bounded loop: model calls per user message, tool calls per model turn, tool result
    # size sent back to the model. Each limit caps tokens, latency and runaway behaviour.
    agent_max_steps: int = Field(default=6, ge=1, le=20)
    agent_max_tool_calls_per_step: int = Field(default=4, ge=1, le=10)
    agent_tool_result_max_chars: int = Field(default=4000, ge=500, le=20000)
    # local: tools run in-process; mcp: through the read-only MCP server (stdio subprocess).
    agent_tool_transport: Literal["local", "mcp"] = "local"
    # "Today" for the agent and its tools. Unset = the real date; the seeded business data
    # is written against 2026-11-15, so demos and evaluations pin it.
    agent_reference_date: date | None = None
    # Local model thinking for the agent (overrides LLM_REASONING). Measured: without it
    # Qwen3 8B read "days_since_delivery: 40" as inside a 30-day window; with it, correct.
    agent_reasoning: bool = True

    # Return window per tenant, in days: the enforced copy of each tenant's active
    # returns policy (NSI-POL-RET-003 v3.0, NSI-EU-POL-RET-001 v1.2); a test checks they
    # agree. The documents explain the rule, this setting enforces it in code, even if
    # the model and the approver both miss it. No entry: no returns for that tenant.
    return_window_days: dict[str, int] = Field(
        default_factory=lambda: {"nsi-amer": 30, "nsi-emea": 21}
    )

    # --- MCP server (`make mcp`): the one identity every tool call runs as ---
    # Fixed at launch, never taken from the model or the client (no confused deputy).
    mcp_tenant_id: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,64}$")
    mcp_user_id: str = Field(default="mcp-client", pattern=r"^[A-Za-z0-9._@-]{1,64}$")
    mcp_role: Literal["support_agent", "finance_analyst", "supervisor", "editor"] | None = None

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
    def _authentication(self) -> Settings:
        # Fail at startup, not on the first request: a deployed API must never trust
        # identity headers, and a JWT setup without issuer or audience is not a check.
        if self.auth_mode == "dev_headers":
            if self.app_env not in ("local", "test"):
                raise ValueError("AUTH_MODE=dev_headers is only allowed in local and test")
            return self
        if not self.auth_jwt_issuer or not self.auth_jwt_audience:
            raise ValueError("AUTH_MODE=jwt requires AUTH_JWT_ISSUER and AUTH_JWT_AUDIENCE")
        if (self.auth_jwks_url is None) == (self.auth_jwks_path is None):
            raise ValueError("AUTH_MODE=jwt requires exactly one of AUTH_JWKS_URL, AUTH_JWKS_PATH")
        if self.auth_jwks_url is not None and not self.auth_jwks_url.startswith("https://"):
            raise ValueError("AUTH_JWKS_URL must use https")
        algorithms = set(self.auth_jwt_algorithms)
        if not algorithms or not algorithms <= ASYMMETRIC_JWT_ALGORITHMS:
            raise ValueError(
                f"AUTH_JWT_ALGORITHMS must be asymmetric: {sorted(ASYMMETRIC_JWT_ALGORITHMS)}"
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
