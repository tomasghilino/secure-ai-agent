# Northstar AI Operations Copilot - developer commands.
# Recipes stay shell-agnostic (no sh-only syntax) so GNU Make works on Linux, macOS and
# Windows (cmd.exe). Run `make help` to list targets.

.DEFAULT_GOAL := help
.PHONY: help setup data dev up down llm-up llm-pull test test-llm lint format typecheck check

OLLAMA_CHAT_MODEL ?= qwen3:8b
OLLAMA_EMBEDDING_MODEL ?= qwen3-embedding:0.6b

help: ## List available commands
	@uv run --no-project --quiet python scripts/make_help.py Makefile

setup: ## Install Python 3.12 deps (all groups) and create .env if missing
	uv sync --all-groups
	uv run python scripts/setup_env.py

data: ## Generate the synthetic PDFs + manifest from data/source (deterministic)
	uv run python -m scripts.generate_pdfs

dev: ## Run the full local stack (PostgreSQL+pgvector, ElasticMQ, API) with hot reload
	docker compose up --build

up: ## Start only infrastructure services (db, sqs) in the background
	docker compose up -d --wait db sqs

down: ## Stop the local stack, including Ollama (data and model volumes are kept)
	docker compose --profile llm down

llm-up: ## Start local Ollama on the NVIDIA GPU (compose profile `llm`)
	docker compose --profile llm up -d --wait ollama

llm-pull: llm-up ## Download the local chat and embedding models (~6 GB, once)
	docker compose exec ollama ollama pull $(OLLAMA_EMBEDDING_MODEL)
	docker compose exec ollama ollama pull $(OLLAMA_CHAT_MODEL)

test: ## Unit tests: no Docker, no AWS, no cost
	uv run pytest tests/unit

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
