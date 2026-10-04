# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS base

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

FROM python:3.14-slim-trixie AS runtime

WORKDIR /app
COPY --from=base /app/.venv /app/.venv
COPY --from=base /app/src /app/src
COPY --from=base /app/alembic.ini /app/alembic.ini
COPY --from=base /app/alembic /app/alembic

ENV PATH="/app/.venv/bin:$PATH"
ENV UVICORN_WORKERS=4

# Drop privileges: run as a non-root system user in the runtime image. The
# venv and source were copied as root and stay read-only to this user. The one
# directory it must write is the local photo backend's default root,
# ./var/photos under WORKDIR — without it every photo upload died on mkdir with
# a PermissionError. A named volume mounted there starts out
# with this ownership, which is what keeps the blobs across a redeploy.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid 1000 --no-create-home app \
    && mkdir -p /app/var/photos /app/var/prometheus \
    && chown -R app:app /app/var
USER app

EXPOSE 8000

# Liveness/readiness probe hitting the app's /health route.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]

# Production: multi-worker. Override UVICORN_WORKERS at deploy time.
#
# Deliberately no --forwarded-allow-ips='*': with '*' uvicorn trusts an
# X-Forwarded-For from every peer and rewrites request.client.host to the
# leftmost, client-supplied entry. The auth rate limiter keys on that host
# (RATE_LIMIT_TRUST_FORWARDED_FOR defaults false), so '*' lets a caller rotate
# the header per request and walk past the login limit — and nginx's
# $proxy_add_x_forwarded_for appends, so the attacker's value stays leftmost
# even behind the proxy. The root Dockerfile made the same call.
#
# Behind a trusted proxy that overwrites the client-IP headers, opt in at deploy
# time instead: FORWARDED_ALLOW_IPS=<proxy-ip-or-cidr> (uvicorn reads that env
# var natively) together with RATE_LIMIT_TRUST_FORWARDED_FOR=true.
CMD ["sh", "-c", "exec uvicorn tripl.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-4}"]
