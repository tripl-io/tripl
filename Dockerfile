# syntax=docker/dockerfile:1
#
# Consolidated single-container image: FastAPI serves the JSON API AND the built
# SPA in one process via app.frontend() (FastAPI 0.138+), replacing the separate
# nginx/frontend container. Build from the repo ROOT:
#
#     docker build -t tripl .
#
# This is the one image tripl ships (release.yml and preview.yml push its
# `runtime` target, and CI builds it). backend/Dockerfile and frontend/Dockerfile
# are the development images compose.dev.yaml runs.

# ---- frontend build -> dist/ ----
# On the build machine's own platform: dist/ is static files, the same for
# every target, so a multi-arch build compiles the SPA once instead of again
# under QEMU for arm64 (that emulated `bun run build` was most of a release).
FROM --platform=$BUILDPLATFORM oven/bun:1.4.2-slim AS frontend-build
WORKDIR /app
COPY frontend/package.json frontend/bun.lock frontend/bunfig.toml ./
RUN --mount=type=cache,id=bun,target=/root/.bun/install/cache \
    bun install --frozen-lockfile
COPY frontend/ ./
RUN bun run build
# The license files of what the bundle is built from; the runtime stage indexes
# them together with the Python and OS packages.
RUN bun scripts/third-party-licenses.mjs /app/licenses

# ---- backend deps + source ----
# uv pinned for a reproducible build; bump it together with backend/Dockerfile,
# mcp-server/Dockerfile and tripl-enterprise's UV_IMAGE.
FROM ghcr.io/astral-sh/uv:0.13.0-python3.14-trixie-slim AS backend-base
WORKDIR /app
ENV UV_LINK_MODE=copy
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra otel --no-install-project
COPY backend/src ./src
COPY backend/alembic.ini ./alembic.ini
COPY backend/alembic ./alembic
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --extra otel

# ---- runtime: API + SPA in one process ----
FROM python:3.14-slim-trixie AS runtime
WORKDIR /app
COPY --from=backend-base /app/.venv /app/.venv
COPY --from=backend-base /app/src /app/src
COPY --from=backend-base /app/alembic.ini /app/alembic.ini
COPY --from=backend-base /app/alembic /app/alembic
# Bake the built SPA in and point the app at it; app.frontend() serves it.
COPY --from=frontend-build /app/dist /app/frontend_dist

# Third-party license notices: /app/licenses/README.md indexes the Python
# packages, the web application's and the Debian packages, with every license
# file next to it. Generated last, from what this image actually contains.
COPY --from=frontend-build /app/licenses /app/licenses
RUN --mount=type=bind,source=backend/scripts/third_party_licenses.py,target=/tmp/third_party_licenses.py \
    /app/.venv/bin/python /tmp/third_party_licenses.py --out /app/licenses

ENV PATH="/app/.venv/bin:$PATH"
ENV UVICORN_WORKERS=4
ENV SERVE_FRONTEND=true
ENV FRONTEND_DIST_DIR=/app/frontend_dist

# The bytecode of the server, its migrations and every installed package. The
# app user cannot write to the source or the virtualenv, so without it every
# process (each uvicorn worker, Celery, the migrate one-shot) compiles everything
# it imports, in memory, at each start. uv's own bytecode option would skip the
# editable /app/src. Tests are left out. A third-party file that does not
# compile is skipped too (-qq, and its exit status ignored), as pip and uv do;
# Python reports it if it is ever imported.
RUN python -m compileall -q -j 0 -x '/tests/' /app/src /app/alembic \
    && { python -m compileall -qq -j 0 -x '/tests/' /app/.venv/lib || true; }

# The local photo backend's default root is ./var/photos under WORKDIR, and the
# app user cannot create anything under the root-owned /app, so every photo
# upload died on mkdir with a PermissionError. Only the photo
# directory is handed to the user; the venv and source stay read-only. A named
# volume mounted here starts out with this ownership, which is what keeps the
# blobs across a redeploy.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid 1000 --no-create-home app \
    && mkdir -p /app/var/photos \
    && mkdir -p /app/var/prometheus \
    && chown -R app:app /app/var
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]

# This single container is the network edge, so we do NOT pass uvicorn
# --forwarded-allow-ips='*': request.client.host is the real peer and the rate
# limiter keys on it (rate_limit_trust_forwarded_for defaults False). If you add
# a trusted proxy/LB in front, add --proxy-headers --forwarded-allow-ips=<proxy>
# and set RATE_LIMIT_TRUST_FORWARDED_FOR=true.
CMD ["sh", "-c", "exec uvicorn tripl.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-4}"]
