"""Swagger UI (``/docs``) and ReDoc (``/redoc``), served by this instance itself.

FastAPI's own pages load their scripts and styles from cdn.jsdelivr.net and
start Swagger UI from an inline ``<script>``. The policy the API puts on every
response when it serves the SPA (``script-src 'self'``, see
:mod:`tripl.middleware.security_headers`) refuses all of that, so both pages
rendered blank on every production install. These are the same two viewers,
with:

* every asset served from :data:`ASSETS_PATH`, out of ``static/``: no CDN, so
  the pages also work on an instance with no route to the internet. Each
  vendored file is listed with its source, checksum and license in
  ``static/third-party.json``; to update one, replace the file from the npm
  tarball named there and update its entry;
* no inline script: Swagger UI starts from ``static/swagger-init.js``, and
  ReDoc starts itself from its ``<redoc spec-url>`` element;
* a policy of their own (:data:`DOCS_CSP`). The middleware never overrides a
  header a response already carries, so the global policy stays as strict as
  it is, and an operator's own ``CONTENT_SECURITY_POLICY`` cannot blank these
  two pages either.
"""

from __future__ import annotations

import html
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from tripl.config import settings

STATIC_DIR = Path(__file__).parent / "static"
ASSETS_PATH = "/docs/assets"

#: What the two pages need, and no more: same-origin scripts, styles and the
#: document fetch, the inline styles both viewers inject, ``data:`` images
#: from Swagger UI's stylesheet, and the ``blob:`` worker ReDoc runs its search
#: index in, the one thing the SPA's policy does not allow.
DOCS_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
    "worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'"
)

_SWAGGER_UI_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{assets}/swagger-ui.css">
</head>
<body>
<div id="swagger-ui" data-openapi-url="{openapi_url}"></div>
<script src="{assets}/swagger-ui-bundle.js"></script>
<script src="{assets}/swagger-init.js"></script>
</body>
</html>
"""

_REDOC_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
<redoc spec-url="{openapi_url}"></redoc>
<script src="{assets}/redoc.standalone.js"></script>
</body>
</html>
"""


def _page(request: Request, template: str, title: str, openapi_url: str) -> HTMLResponse:
    # The prefix the app is mounted under (uvicorn --root-path), as FastAPI's
    # own handlers apply it, so both URLs resolve behind a path-routing proxy.
    root = str(request.scope.get("root_path", "")).rstrip("/")
    body = template.format(
        title=html.escape(title),
        assets=html.escape(root + ASSETS_PATH),
        openapi_url=html.escape(root + openapi_url),
    )
    headers = {"content-security-policy": DOCS_CSP} if settings.security_headers_enabled else {}
    return HTMLResponse(body, headers=headers)


def install_pages(app: FastAPI) -> None:
    """``/docs``, ``/redoc`` and their assets, for an app built with both URLs off.

    Nothing to serve when the app publishes no document (``openapi_url=None``).
    """
    openapi_url = app.openapi_url
    if openapi_url is None:
        return
    app.mount(ASSETS_PATH, StaticFiles(directory=STATIC_DIR), name="api-docs-assets")

    async def swagger_ui(request: Request) -> HTMLResponse:
        return _page(request, _SWAGGER_UI_PAGE, f"{app.title} - Swagger UI", openapi_url)

    async def redoc(request: Request) -> HTMLResponse:
        return _page(request, _REDOC_PAGE, f"{app.title} - ReDoc", openapi_url)

    # Plain routes, as FastAPI registers its own pages: not API operations, so
    # the route audits that walk APIRoutes (auth, organization fences) skip them.
    app.add_route("/docs", swagger_ui, include_in_schema=False)
    app.add_route("/redoc", redoc, include_in_schema=False)
