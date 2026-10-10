"""The legacy ``/settings`` routes the web app no longer calls are marked deprecated.

The per-scope surfaces (``/orgs/{org}/settings``, ``/platform/settings``)
replaced them. The prompt defaults and the two limits stay current: the web app
still reads them from ``/settings``.
"""

from __future__ import annotations

from tripl.main import app

_DEPRECATED = (
    ("get", "/api/v1/settings"),
    ("patch", "/api/v1/settings"),
    ("put", "/api/v1/settings"),
    ("get", "/api/v1/settings/ai"),
    ("post", "/api/v1/settings/ai/test"),
    ("post", "/api/v1/settings/email/test"),
)
_CURRENT = (
    ("get", "/api/v1/settings/ai/defaults"),
    ("get", "/api/v1/settings/photo-limits"),
    ("get", "/api/v1/settings/row-limits"),
)


def test_the_unused_legacy_routes_are_deprecated_and_the_used_ones_are_not() -> None:
    paths = app.openapi()["paths"]
    for method, path in _DEPRECATED:
        assert paths[path][method].get("deprecated") is True, (method, path)
    for method, path in _CURRENT:
        assert not paths[path][method].get("deprecated"), (method, path)
