# Northstar AI Operations Copilot - developer commands.
# Recipes stay shell-agnostic (no sh-only syntax) so GNU Make works on Linux, macOS and
# Windows (cmd.exe). Run `make help` to list targets.

.DEFAULT_GOAL := help
.PHONY: help setup data dev up down migrate migration seed ingest worker search ask agent approve mcp token eval-retrieval eval-answers eval-security llm-up llm-pull test test-integration test-llm lint format typecheck check image scan tf-check staging-plan staging-images

OLLAMA_CHAT_MODEL ?= qwen3:8b
OLLAMA_EMBEDDING_MODEL ?= qwen3-embedding:0.6b
# Git Bash would rewrite container paths such as /repo into Windows paths.
export MSYS_NO_PATHCONV := 1
# Security scanners, pinned by digest (same images as CI).
GITLEAKS_IMAGE := ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
TRIVY_IMAGE := ghcr.io/aquasecurity/trivy:0.76.2@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb
IMAGE ?= secure-ai-agent:local
# Development identity for `make agent`, `make mcp` and `make token`.
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

token: ## Mint a local JWT for the API (AUTH_MODE=jwt): make token [role=supervisor user=sup-1]
	uv run python -m scripts.dev_token --tenant $(tenant) --role $(role) --user $(user)

eval-retrieval: ## Retrieval metrics (dense vs keyword vs hybrid) on data/evaluation; free
	uv run python -m scripts.evaluate_retrieval

eval-answers: ## Answer checks (citations, refusals) on data/evaluation with the chat model
	uv run python -m scripts.evaluate_answers

eval-security: ## AI red-team suite against the agent (injection, cross-tenant, tool abuse...)
	uv run python -m scripts.evaluate_security

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

image: ## Build the hardened production image (Dockerfile target `runtime`)
	docker build --target runtime -t $(IMAGE) .

scan: image ## Security scans as in CI: secrets in history, dependency CVEs, misconfigurations, image CVEs
	docker run --rm -v "$(CURDIR):/repo" $(GITLEAKS_IMAGE) git /repo --redact --no-banner
	uv export --frozen --no-dev --no-emit-project --quiet --output-file reports/requirements.txt
	uvx pip-audit@2.10.1 --requirement reports/requirements.txt --require-hashes --disable-pip --strict
	docker run --rm -v "$(CURDIR):/src:ro" -v trivy-cache:/root/.cache/ $(TRIVY_IMAGE) fs --quiet --scanners misconfig,secret --severity HIGH,CRITICAL --exit-code 1 --skip-dirs /src/.venv --skip-dirs /src/.auth --skip-dirs /src/notes --skip-dirs /src/reports --skip-dirs /src/infra/terraform/.terraform /src
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v trivy-cache:/root/.cache/ $(TRIVY_IMAGE) image --quiet --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 $(IMAGE)

# --- AWS staging (Terraform). Plans and applies need an AWS login; see docs/operations/staging.md
TF := terraform -chdir=infra/terraform
# Evaluated only by the targets that use them.
STAGING_REPOSITORY = $(shell $(TF) output -raw ecr_repository_url)
STAGING_TAG = $(shell git rev-parse --short=12 HEAD)

tf-check: ## Terraform format and validation (no AWS access needed)
	$(TF) fmt -check -recursive
	$(TF) init -backend=false -input=false
	$(TF) validate

staging-plan: ## Show what applying would change in AWS staging (needs an AWS login; changes nothing)
	$(TF) init -input=false
	$(TF) plan -out=staging.tfplan

staging-images: ## Build, scan and push the runtime and ops images (tag = current commit)
	docker build --target runtime -t $(STAGING_REPOSITORY):$(STAGING_TAG) .
	docker build --target ops -t $(STAGING_REPOSITORY):ops-$(STAGING_TAG) .
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v trivy-cache:/root/.cache/ $(TRIVY_IMAGE) image --quiet --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 $(STAGING_REPOSITORY):$(STAGING_TAG)
	aws ecr get-login-password | docker login --username AWS --password-stdin $(firstword $(subst /, ,$(STAGING_REPOSITORY)))
	docker push $(STAGING_REPOSITORY):$(STAGING_TAG)
	docker push $(STAGING_REPOSITORY):ops-$(STAGING_TAG)
	@echo Pushed $(STAGING_TAG): set image_tag = "$(STAGING_TAG)" in infra/terraform/terraform.tfvars
