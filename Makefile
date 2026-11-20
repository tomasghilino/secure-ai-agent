# Northstar AI Operations Copilot - developer commands.
# Recipes stay shell-agnostic (no sh-only syntax) so GNU Make works on Linux, macOS and
# Windows (cmd.exe). Run `make help` to list targets.

.DEFAULT_GOAL := help
.PHONY: help setup data dev up down migrate migration seed ingest worker search ask agent approve mcp eval-retrieval eval-answers llm-up llm-pull test test-integration test-llm lint format typecheck check

OLLAMA_CHAT_MODEL ?= qwen3:8b
OLLAMA_EMBEDDING_MODEL ?= qwen3-embedding:0.6b
# Development identity for `make agent` / `make mcp` (to be replaced by JWT authentication).
tenant ?= nsi-amer
role ?= support_agent
user ?= agent-1

help: ## List available commands
	@uv run --no-project --quiet python scripts/make_help.py Makefile

setup: ## Install Python 3.12 deps (all groups) and create .env if missing
	uv sync --all-groups
	uv run python scripts/setup_env.py

data: ## Generate the synthetic PDFs + manifest from data/source (deterministic)
	uv run python -m scripts.generate_pdfs

dev: ## Run the full local stack (PostgreSQL+pgvector, ElasticMQ, API, worker)
	docker compose up --build

up: ## Start only infrastructure services (db, sqs) in the background
	docker compose up -d --wait db sqs

down: ## Stop the local stack, including Ollama (data and model volumes are kept)
	docker compose --profile llm down

migrate: ## Apply pending database migrations (needs `make up`)
	uv run alembic upgrade head

migration: ## Draft a migration from the models: make migration m="add x" (review it!)
	uv run alembic revision --autogenerate -m "$(m)"

seed: ## Load the business records (customers, orders, invoices...) from data/source/business
	uv run python -m scripts.seed_business_data

ingest: ## Ingest the generated PDFs into PostgreSQL (needs up, migrate, data; idempotent)
	uv run python -m scripts.ingest_samples

worker: ## Run the ingestion worker on the host (needs up, migrate; Ctrl+C stops it)
	uv run python -m app.ingestion.worker

search: ## Hybrid search over the ingested chunks: make search q="your question"
	uv run python -m scripts.search "$(q)"

ask: ## Answer a question with citations (RAG): make ask q="your question"
	uv run python -m scripts.ask "$(q)"

agent: ## Ask the operations agent: make agent q="..." [role=finance_analyst user=fin-1]
	uv run python -m scripts.agent "$(q)" --tenant $(tenant) --role $(role) --user $(user)

approve: ## Decide a pending agent action as supervisor sup-1: make approve t=<thread> [reject=1]
	uv run python -m scripts.agent --decide $(t) --tenant $(tenant) --role supervisor --user sup-1 $(if $(reject),--reject,)

mcp: ## Run the read-only MCP server on stdio: make mcp [tenant=... role=...] (for MCP clients)
	uv run python -m app.mcp_server --tenant $(tenant) --role $(role) --user $(user)

eval-retrieval: ## Retrieval metrics (dense vs keyword vs hybrid) on data/evaluation; free
	uv run python -m scripts.evaluate_retrieval

eval-answers: ## Answer checks (citations, refusals) on data/evaluation with the chat model
	uv run python -m scripts.evaluate_answers

llm-up: ## Start local Ollama on the NVIDIA GPU (compose profile `llm`)
	docker compose --profile llm up -d --wait ollama

llm-pull: llm-up ## Download the local chat and embedding models (~6 GB, once)
	docker compose exec ollama ollama pull $(OLLAMA_EMBEDDING_MODEL)
	docker compose exec ollama ollama pull $(OLLAMA_CHAT_MODEL)

test: ## Unit tests: no Docker, no AWS, no cost
	uv run pytest tests/unit

test-integration: ## Integration tests against local PostgreSQL (needs `make up`; free)
	uv run pytest -m integration

test-llm: ## Real-model tests against local Ollama (free; skipped if Ollama is down)
	uv run pytest -m llm

lint: ## Lint and verify formatting
	uv run ruff check .
	uv run ruff format --check .

format: ## Auto-format and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Static type checking (mypy --strict)
	uv run mypy

check: lint typecheck test ## Everything CI runs locally
