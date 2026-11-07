# Northstar AI Operations Copilot - developer commands.
# Recipes stay shell-agnostic (no sh-only syntax) so GNU Make works on Linux, macOS and
# Windows (cmd.exe). Run `make help` to list targets.

.DEFAULT_GOAL := help
.PHONY: help setup dev up down test lint format typecheck check

help: ## List available commands
	@uv run --no-project --quiet python scripts/make_help.py Makefile

setup: ## Install Python 3.12 deps (all groups) and create .env if missing
	uv sync --all-groups
	uv run python scripts/setup_env.py

dev: ## Run the full local stack (PostgreSQL+pgvector, ElasticMQ, API) with hot reload
	docker compose up --build

up: ## Start only infrastructure services (db, sqs) in the background
	docker compose up -d --wait db sqs

down: ## Stop the local stack (data volumes are kept)
	docker compose down

test: ## Unit tests: no Docker, no AWS, no cost
	uv run pytest tests/unit

lint: ## Lint and verify formatting
	uv run ruff check .
	uv run ruff format --check .

format: ## Auto-format and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Static type checking (mypy --strict)
	uv run mypy

check: lint typecheck test ## Everything CI runs locally
