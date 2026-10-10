"""The error body every API route answers with, as the OpenAPI document shows it."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorResponse(BaseModel):
    """``{"detail": ...}``: FastAPI's error shape, with the variants tripl adds.

    ``detail`` is a message for most errors; a list of field errors on a 422;
    an object where a route has more to say than a message (a merge's
    ``conflicts``, an import's ``errors``). A refusal from an organization's
    gate adds keys beside ``detail`` (``sso_start``), and a 500 adds
    ``request_id``, to quote when reporting it.
    """

    model_config = ConfigDict(extra="allow")

    detail: str | list[Any] | dict[str, Any]
    request_id: str | None = Field(
        default=None, description="On a 500: the id the server logged it under."
    )


def _error(description: str) -> dict[str, Any]:
    return {"model": ErrorResponse, "description": description}


#: What any route behind ``get_current_user`` may answer besides its own codes.
AUTH_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: _error("No session or API key, or the API key is invalid, expired or revoked."),
    403: _error("Signed in, but this account or API key may not do this."),
}

#: :data:`AUTH_ERROR_RESPONSES`, plus the 404 a ``/projects/{slug}`` route answers
#: for a project that does not exist or that the caller is not a member of.
PROJECT_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **AUTH_ERROR_RESPONSES,
    404: _error("No such project (or the caller is not a member of it), or no such object in it."),
}
