# syntax=docker/dockerfile:1.7
# Multi-stage image: `dev` (hot reload, dev deps) and `runtime` (slim, non-root).

FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Keep the venv outside /app so a dev bind mount of the source never hides it.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/
WORKDIR /app

# --- Production dependencies only (cached unless the lockfile changes) ---
FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# --- Local development: adds dev tools; source is bind-mounted by docker compose ---
FROM deps AS dev
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# --- Runtime: minimal, non-root ---
FROM base AS runtime
RUN useradd --create-home --uid 10001 appuser
COPY --from=deps /opt/venv /opt/venv
COPY app ./app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
