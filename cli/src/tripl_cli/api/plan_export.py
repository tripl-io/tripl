"""Exporting the plan as a JSON Schema bundle or as the model ``tripl codegen`` renders.

A plain GET that any project member may call. ``?branch=`` follows the rule
every plan read follows (see ``api/branches.py``): omitted means the live main
plan. ``format`` is sent explicitly even for the route's default, so a future
default change on the server can never silently change what a pinned CI step
generates.
"""

from __future__ import annotations

from typing import Any

from tripl_cli.api.request import ApiRequest
from tripl_cli.model import JsonDict, as_dict

EXPORT = "/projects/{slug}/plan/export"

ENDPOINTS: tuple[tuple[str, str], ...] = (("get", EXPORT),)

FORMAT_JSONSCHEMA = "jsonschema"
FORMAT_CODEGEN_MODEL = "codegen_model"
FORMATS: tuple[str, ...] = (FORMAT_JSONSCHEMA, FORMAT_CODEGEN_MODEL)


def export(slug: str, export_format: str, *, branch: str | None = None) -> ApiRequest:
    """One export. ``export_format`` is one of ``FORMATS``."""
    if export_format not in FORMATS:
        raise ValueError(f"unknown export format {export_format!r}; expected one of {FORMATS}")
    return ApiRequest(
        "GET",
        EXPORT.format(slug=slug),
        params={"format": export_format, "branch": branch},
    )


def document(payload: Any) -> JsonDict:
    """The export body as a dict; anything else reads as empty."""
    return as_dict(payload)
