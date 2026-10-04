"""Serving the built SPA from FastAPI via app.frontend().

The consolidated single-container prod deploy serves the SPA from the API process
using FastAPI 0.138+'s app.frontend(). These tests pin the behavior that makes
that safe: API path operations keep precedence, unmatched *navigation* paths fall
back to index.html for client-side routing, static assets are served, and —
because the SPA now flows through SecurityHeadersMiddleware — a CSP is applied
when serve_frontend is on. The tests use throwaway apps so they don't depend on
the main app's serve_frontend flag (off by default in dev/tests).

Since FastAPI 0.139.1 the index.html fallback only applies to navigation
requests — ones whose Accept header admits text/html or application/xhtml+xml.
Anything else (fetch/XHR with Accept: */*, a mistyped asset URL, a probe) gets a
real 404 instead of an HTML body under a 200. That is the behavior we want, so
the fallback tests below send a browser-like Accept header explicitly rather than
relying on the httpx client default of ``*/*``.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tripl.config import settings
from tripl.middleware import SecurityHeadersMiddleware

# What a browser sends when the user navigates to a URL (Chrome's Accept header).
# app.frontend()'s index.html fallback is gated on this; the httpx default of
# ``*/*`` deliberately does NOT qualify.
NAVIGATION_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    )
}


def _make_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>tripl SPA</title>")
    (dist / "assets" / "app.js").write_text("console.log('app')")
    return dist


def _spa_app(dist: Path) -> FastAPI:
    app = FastAPI()

    @app.get("/api/ping")
    def ping() -> dict[str, str]:
        return {"pong": "1"}

    app.frontend("/", directory=str(dist), fallback="index.html")
    return app


def test_api_routes_take_precedence_over_spa(tmp_path: Path) -> None:
    resp = TestClient(_spa_app(_make_dist(tmp_path))).get("/api/ping")
    assert resp.status_code == 200
    assert resp.json() == {"pong": "1"}


def test_root_serves_index_html(tmp_path: Path) -> None:
    resp = TestClient(_spa_app(_make_dist(tmp_path))).get("/")
    assert resp.status_code == 200
    assert "tripl SPA" in resp.text


def test_client_side_route_falls_back_to_index(tmp_path: Path) -> None:
    resp = TestClient(_spa_app(_make_dist(tmp_path))).get(
        "/projects/deep/route", headers=NAVIGATION_HEADERS
    )
    assert resp.status_code == 200
    assert "tripl SPA" in resp.text


def test_non_navigation_request_to_unknown_path_is_404(tmp_path: Path) -> None:
    # A fetch/XHR or asset probe must NOT be answered with the SPA shell under a
    # 200 — that turns a broken request into a silently "successful" HTML body.
    client = TestClient(_spa_app(_make_dist(tmp_path)))
    for accept in ("*/*", "application/json"):
        resp = client.get("/projects/deep/route", headers={"accept": accept})
        assert resp.status_code == 404, accept


def test_static_asset_is_served(tmp_path: Path) -> None:
    resp = TestClient(_spa_app(_make_dist(tmp_path))).get("/assets/app.js")
    assert resp.status_code == 200
    assert "console.log" in resp.text


def test_default_spa_csp_applied_when_serving_frontend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "serve_frontend", True)
    monkeypatch.setattr(settings, "content_security_policy", "")
    app = _spa_app(_make_dist(tmp_path))
    app.add_middleware(SecurityHeadersMiddleware)
    csp = TestClient(app).get("/").headers.get("content-security-policy")
    assert csp == (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; "
        "connect-src 'self'; frame-src https://www.figma.com https://embed.figma.com; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
