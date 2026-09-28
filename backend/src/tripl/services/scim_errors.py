"""SCIM protocol errors (RFC 7644 §3.12), raised by the SCIM services and routes.

``tripl.main`` answers :class:`ScimError` as the SCIM error body, with the
``application/scim+json`` media type::

    {"schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
     "status": "400", "scimType": "invalidValue", "detail": "..."}

``status`` is a string, as the RFC's examples spell it.
"""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse

ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_MEDIA_TYPE = "application/scim+json"
#: Every SCIM request's path starts with this; errors under it use the SCIM format.
SCIM_PATH_PREFIX = "/scim/v2/"

# The scimType values of RFC 7644 Table 9 this implementation answers with.
INVALID_FILTER = "invalidFilter"
INVALID_SYNTAX = "invalidSyntax"
INVALID_PATH = "invalidPath"
NO_TARGET = "noTarget"
INVALID_VALUE = "invalidValue"
MUTABILITY = "mutability"
UNIQUENESS = "uniqueness"
TOO_MANY = "tooMany"


class ScimError(Exception):
    """An error answered in the SCIM error format."""

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        scim_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type
        self.headers = headers or {}


def bad_request(detail: str, scim_type: str = INVALID_VALUE) -> ScimError:
    return ScimError(400, detail, scim_type=scim_type)


def not_found(detail: str = "Resource not found") -> ScimError:
    return ScimError(404, detail)


def conflict(detail: str, scim_type: str = UNIQUENESS) -> ScimError:
    return ScimError(409, detail, scim_type=scim_type)


def error_body(exc: ScimError) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemas": [ERROR_SCHEMA],
        "status": str(exc.status),
        "detail": exc.detail,
    }
    if exc.scim_type is not None:
        body["scimType"] = exc.scim_type
    return body


def error_response(exc: ScimError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content=error_body(exc),
        media_type=SCIM_MEDIA_TYPE,
        headers=exc.headers,
    )
