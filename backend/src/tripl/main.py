from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from brotli_asgi import BrotliMiddleware
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from tripl.config import settings
from tripl.services.app_settings_service import apply_startup_service_overrides

# Apply persisted Security/Storage/Observability overrides onto `settings` before
# anything reads them: the middleware stack, auth rate limiters, logging config
# and the /metrics route are all wired from `settings` at import time below, so
# this has to run first (a later apply would be ignored). This is what makes
# those overrides "take effect on the next deploy", as the settings UI states.
apply_startup_service_overrides()

from tripl.api.v1.router import router as v1_router  # noqa: E402
from tripl.database import engine  # noqa: E402
from tripl.logging_config import configure_logging  # noqa: E402
from tripl.middleware import (  # noqa: E402
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
    StaticCacheMiddleware,
)
from tripl.middleware.body_limit import BodyLimitMiddleware  # noqa: E402
from tripl.middleware.request_id import bound_request_id, request_id_from_scope  # noqa: E402
from tripl.middleware.security_headers import build_security_headers  # noqa: E402
from tripl.observability.metrics import render_metrics  # noqa: E402

# Configure logging now (after overrides are applied) so every log line — including
# those emitted while building the app and importing routers, before the async
# lifespan runs — uses the structured handler and the effective log level.
# configure_logging() is idempotent, so the call in the lifespan below simply
# re-applies the current settings.
configure_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: re-apply logging config, fail fast on misconfigured production deploys."""
    configure_logging()
    settings.assert_production_ready()
    logger.info(
        "tripl starting",
        extra={
            "debug": settings.debug,
            "cors_origins": settings.cors_origins(),
            "rate_limit": settings.rate_limit_enabled,
        },
    )
    yield


# One entry per tag actually used across the v1 routers (see api/v1/*.py).
# Drives the grouping and short blurbs in the generated OpenAPI docs.
_OPENAPI_TAGS = [
    {"name": "auth", "description": "Login, registration, logout, and current-user lookup."},
    {"name": "users", "description": "User administration and role management."},
    {"name": "projects", "description": "Projects (tracking plans) and their lifecycle."},
    {"name": "events", "description": "Tracked events within a project's plan."},
    {"name": "event-types", "description": "Event type definitions and metadata."},
    {"name": "event-type-owners", "description": "Ownership assignments for event types."},
    {"name": "fields", "description": "Field definitions attached to event types."},
    {"name": "meta-fields", "description": "Project-wide meta/context fields."},
    {"name": "variables", "description": "Reusable variables referenced by the plan."},
    {"name": "relations", "description": "Relationships between plan entities."},
    {"name": "scans", "description": "Scan configs and warehouse scan/preview jobs."},
    {"name": "metrics", "description": "Computed metrics and metric definitions."},
    {"name": "reconciliation", "description": "Plan-vs-warehouse reconciliation runs."},
    {"name": "plan-branches", "description": "Working branches of a tracking plan."},
    {
        "name": "dependencies",
        "description": "What depends on a plan entity, and what a planned change affects.",
    },
    {"name": "plan-revisions", "description": "Committed plan revisions and history."},
    {"name": "chart-annotations", "description": "Annotations overlaid on metric charts."},
    {"name": "anomaly-settings", "description": "Per-project anomaly detection settings."},
    {"name": "alerting", "description": "Alert rules and delivery destinations."},
    {"name": "data-sources", "description": "Warehouse/data-source connections and secrets."},
    {"name": "event-photos", "description": "Photo attachments for events."},
    {"name": "search", "description": "Hybrid lexical/semantic search over plan content."},
    {"name": "ai", "description": "AI-assisted descriptions and Q&A."},
    {"name": "activity", "description": "Recent activity feed."},
    {"name": "audit", "description": "Audit log of mutating actions."},
    {"name": "api-keys", "description": "Personal API keys for programmatic access."},
    {"name": "settings", "description": "Instance-level application settings."},
]

# No `servers=` here, and none injected per request either — this instance never
# tells a client where it lives. An OpenAPI document with no `servers` defaults to
# `[{"url": "/"}]` resolved against the URL the document was fetched from (OpenAPI
# 3.1, OpenAPI Object), so generated clients and the /docs "Try it out" panel use the
# origin they already reached this instance on. That is right for every origin the
# instance answers on — public hostname, internal address, localhost in dev — and
# needs no configuration to stay right.
#
# The alternative, advertising `app_base_url`, shipped and then failed in
# production: the spec was served over https and advertised the plaintext internal
# address the deployment happened to have in APP_BASE_URL, so "Try it out" issued a
# cross-origin request the browser blocked as mixed content and CORS would have
# refused anyway — `Settings.cors_origins()` derives the allow-list from that same
# value, so a wrong `app_base_url` breaks both halves in the same direction
# (tripl-ouxw). Resolving it per request from the Settings -> Runtime override only
# made the wrong value correctable without a restart (tripl-mfqm); no single
# absolute URL is right for an instance reachable at several origins.
#
# It also keeps the served document identical to the committed backend/openapi.json
# that bin/dump-openapi.sh writes and test_openapi_contract pins, instead of a
# second, near-identical spelling of the same spec that only exists over HTTP.
# FastAPI's own /openapi.json handler adds the --root-path prefix and nothing else,
# and only when the app is mounted under one.
app = FastAPI(
    title="tripl",
    version="0.1.0",
    description="Analytics tracking plan service",
    lifespan=lifespan,
    openapi_tags=_OPENAPI_TAGS,
)

# Opt-in OpenTelemetry tracing — gated on OTEL_EXPORTER_OTLP_ENDPOINT env;
# graceful no-op when the env is empty or the otel packages aren't installed.
from tripl.observability.tracing import setup_api_tracing  # noqa: E402

setup_api_tracing(app)

# The last add_middleware call is outermost. The effective user-middleware order
# is CORS -> BodyLimit -> Brotli -> RequestID -> StaticCache -> SecurityHeaders.
# - RequestID assigns/propagates the id for requests that reach the router.
# - SecurityHeaders wraps router responses, including HTTPException and
#   validation errors. CORS preflights and early body-limit 413s skip it.
#   Unhandled 500s are another gap:
#   Starlette writes those above this entire stack, so unhandled_exception_handler
#   re-attaches the same headers itself from build_security_headers (tripl-qu9m).
# - CORS is outermost so preflight requests skip the other middleware.
# - Brotli compresses the final response body (≥1KB), except the project SSE
#   stream where compression adds buffering risk to latency-sensitive chunks.
if settings.security_headers_enabled:
    app.add_middleware(SecurityHeadersMiddleware)
# Cache-Control for the SPA build. app.frontend() sets etag/last-modified but no
# freshness, which lets a browser keep serving a stale index.html for hours after
# a deploy and then 404 on chunk names the new build no longer has. No-op unless
# serve_frontend is on.
app.add_middleware(StaticCacheMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    BrotliMiddleware,
    quality=4,
    minimum_size=1024,
    excluded_handlers=[r"^/api/v1/projects/[^/]+/events/stream$"],
)
app.add_middleware(BodyLimitMiddleware)

_cors_origins = settings.cors_origins()
# allow_credentials=True with "*" is rejected by browsers; fall back to no
# credentials in that case so the dev server still works.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", settings.request_id_header],
    expose_headers=[settings.request_id_header],
    max_age=600,
)

app.include_router(v1_router)


# Serve the built SPA from this same process when enabled — a single-container
# production deploy (no separate static/nginx tier). app.frontend() (FastAPI
# 0.138+) registers low-priority routes: the path operations above are matched
# first, so /api/v1/*, /health, /metrics, and /docs keep precedence; only
# unmatched paths fall through to the SPA, with fallback="index.html" serving
# client-side routes. Since FastAPI 0.139.1 that fallback applies only to
# navigation requests (Accept admits text/html), so browser deep links still
# resolve while a fetch/XHR or mistyped asset URL gets a real 404 instead of the
# SPA shell under a 200. The SPA responses pass through SecurityHeadersMiddleware
# and BrotliMiddleware like any other. Off in dev (Vite serves the SPA with HMR).
if settings.serve_frontend and settings.frontend_dist_dir:
    app.frontend("/", directory=settings.frontend_dist_dir, fallback="index.html")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unhandled exceptions: log with the request id and return a
    generic 500 so internal details never leak to the client.

    FastAPI handles ``HTTPException`` and validation errors before this; only
    truly unexpected errors reach here. The request id (set by
    ``RequestIDMiddleware``) is attached to the structured log line and echoed
    in the body so an operator can correlate the report with the logs.

    Everything below exists because this handler runs from Starlette's
    ServerErrorMiddleware, which wraps the app from *outside* every middleware
    added above — the 500 never travels back through them, so whatever they add
    on the way out has to be reproduced here (tripl-qu9m). Concretely: the id is
    recovered from the ASGI scope because ``RequestIDMiddleware`` has already
    reset its contextvar by now, and the security headers come from the same
    builder that middleware uses so the two lists cannot drift apart.
    """
    request_id = request_id_from_scope(request.scope)
    log_id = request_id or "-"
    with bound_request_id(log_id):
        logger.exception(
            "unhandled exception",
            extra={"method": request.method, "path": request.url.path},
        )
    headers = build_security_headers()
    if request_id is not None:
        # Omitted rather than sent as "-" when unknown (an exception from a
        # middleware layered outside RequestIDMiddleware): log shippers index on
        # this header, and a placeholder would collapse unrelated requests onto
        # one id — worse than an absent header.
        headers[settings.request_id_header] = request_id
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": log_id},
        headers=headers,
    )


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """Liveness + DB-reachability probe. Returns 503 if the DB is unreachable."""
    try:
        async with asyncio.timeout(1.0):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001  — any DB failure should down the probe
        # Log the underlying error server-side, but return a generic body: the
        # /health probe is unauthenticated and the exception text can leak the
        # DSN, driver, and host details.
        logger.exception("health check: database unreachable")
        return JSONResponse(
            status_code=503,
            content={"status": "error", "component": "database"},
        )
    return JSONResponse(content={"status": "ok"})


if settings.prometheus_metrics_enabled:

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> Response:
        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)
