# syntax=docker/dockerfile:1.7
# Multi-stage image:
#   dev      hot reload and dev tools; the source is bind-mounted by docker compose
#   runtime  production: Python, the locked dependencies and the code, nothing else
#   ops      runtime + the seeding scripts and source data, for one-off admin tasks
# Base images are pinned by digest (Dependabot proposes updates), so a rebuild never pulls
# a different image than the one that was scanned.

ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Keep the venv outside /app so a dev bind mount of the source never hides it.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
COPY --from=uv /uv /uvx /bin/
WORKDIR /app

# --- Production dependencies only (cached unless the lockfile changes) ---
FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# --- Local development: adds dev tools ---
FROM deps AS dev
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --all-groups
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# --- Runtime: no uv, no build tools, non-root, read-only friendly ---
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app
# Debian security updates applied at build time (the base digest only moves weekly). Then
# a system user without a shell or home; setuid/setgid bits removed from every binary and
# pip uninstalled, so nothing in the image can raise privileges or install packages, even
# if the process is compromised.
RUN apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
    && mkdir -p /app/data/storage && chown app:app /app/data/storage \
    && python -m pip uninstall --yes --quiet pip
COPY --from=deps /opt/venv /opt/venv
# Code owned by root and compiled at build time: the app user cannot modify it, and the
# container runs with a read-only root filesystem (writable: uploads volume, /tmp).
COPY alembic.ini ./
COPY app ./app
RUN python -m compileall -q app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]

# --- Ops: one-off administrative tasks (migrations, seeding), same hardening as runtime ---
FROM deps AS ops-deps
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --group datagen

FROM runtime AS ops
USER root
COPY --from=ops-deps /opt/venv /opt/venv
COPY scripts ./scripts
COPY data/source ./data/source
RUN python -m compileall -q scripts
USER app
CMD ["alembic", "upgrade", "head"]
