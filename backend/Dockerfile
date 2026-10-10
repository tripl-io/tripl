# syntax=docker/dockerfile:1
#
# The backend's development image. compose.dev.yaml builds the `dev` target
# for the api, celery-worker and celery-beat services and syncs ./src into it
# as you edit. The image tripl ships is the root Dockerfile's `runtime` target,
# which serves the API and the SPA from one process.

# uv pinned for a reproducible build; bump it together with the root
# Dockerfile, mcp-server/Dockerfile and tripl-enterprise's UV_IMAGE.
FROM ghcr.io/astral-sh/uv:0.13.0-python3.14-trixie-slim AS base

WORKDIR /app
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra otel --no-install-project

COPY src ./src
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra otel

FROM base AS dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "tripl.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
