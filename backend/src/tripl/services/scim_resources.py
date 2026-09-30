"""SCIM 2.0 wire vocabulary: schema URNs, discovery documents, list/filter/paging (F20).

What tripl implements of RFC 7643/7644, and what it advertises in
``/ServiceProviderConfig``: PATCH yes; bulk, sort, ETags and password change
no; filtering yes, one ``eq`` comparison per request, at most
:data:`MAX_RESULTS` resources per page.

Supported filters (attribute names and ``eq`` case-insensitive):

* Users: ``userName eq "x"``, ``externalId eq "x"``, ``id eq "x"``,
  ``emails.value eq "x"`` and ``emails[type eq "work"].value eq "x"``.
* Groups: ``displayName eq "x"``, ``externalId eq "x"``, ``id eq "x"``.

Anything else is 400 ``invalidFilter``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tripl.services.scim_errors import (
    INVALID_FILTER,
    INVALID_PATH,
    INVALID_SYNTAX,
    INVALID_VALUE,
    NO_TARGET,
    bad_request,
)

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SPC_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
RESOURCE_TYPE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"

MAX_RESULTS = 200
DEFAULT_COUNT = 100
#: Longest filter expression accepted.
MAX_FILTER_LENGTH = 1024
#: Most PATCH operations accepted in one request.
MAX_OPERATIONS = 1000


# ── discovery ───────────────────────────────────────────────────────────────


def service_provider_config(base_url: str) -> dict[str, Any]:
    return {
        "schemas": [SPC_SCHEMA],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": MAX_RESULTS},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "OAuth Bearer Token",
                "description": "A SCIM token an organization owner creates in tripl",
                "primary": True,
            }
        ],
        "meta": {
            "resourceType": "ServiceProviderConfig",
            "location": f"{base_url}/ServiceProviderConfig",
        },
    }


def _resource_type(base_url: str, name: str, endpoint: str, schema: str) -> dict[str, Any]:
    return {
        "schemas": [RESOURCE_TYPE_SCHEMA],
        "id": name,
        "name": name,
        "endpoint": endpoint,
        "schema": schema,
        "meta": {"resourceType": "ResourceType", "location": f"{base_url}/ResourceTypes/{name}"},
    }


def resource_types(base_url: str) -> list[dict[str, Any]]:
    return [
        _resource_type(base_url, "User", "/Users", USER_SCHEMA),
        _resource_type(base_url, "Group", "/Groups", GROUP_SCHEMA),
    ]


def _attribute(
    name: str,
    *,
    type_: str = "string",
    multi: bool = False,
    required: bool = False,
    mutability: str = "readWrite",
    uniqueness: str = "none",
    sub: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    attr: dict[str, Any] = {
        "name": name,
        "type": type_,
        "multiValued": multi,
        "required": required,
        "caseExact": False,
        "mutability": mutability,
        "returned": "default",
        "uniqueness": uniqueness,
    }
    if sub is not None:
        attr["subAttributes"] = sub
    return attr


def schemas(base_url: str) -> list[dict[str, Any]]:
    user = {
        "schemas": [SCHEMA_SCHEMA],
        "id": USER_SCHEMA,
        "name": "User",
        "description": "A tripl account provisioned into this organization",
        "attributes": [
            _attribute("userName", required=True, uniqueness="server", mutability="immutable"),
            _attribute(
                "name",
                type_="complex",
                sub=[
                    _attribute("formatted"),
                    _attribute("givenName"),
                    _attribute("familyName"),
                ],
            ),
            _attribute("displayName"),
            _attribute(
                "emails",
                type_="complex",
                multi=True,
                sub=[
                    _attribute("value", mutability="immutable"),
                    _attribute("type"),
                    _attribute("primary", type_="boolean"),
                ],
            ),
            _attribute("active", type_="boolean"),
            _attribute(
                "groups",
                type_="complex",
                multi=True,
                mutability="readOnly",
                sub=[_attribute("value", mutability="readOnly"), _attribute("display")],
            ),
        ],
        "meta": {"resourceType": "Schema", "location": f"{base_url}/Schemas/{USER_SCHEMA}"},
    }
    group = {
        "schemas": [SCHEMA_SCHEMA],
        "id": GROUP_SCHEMA,
        "name": "Group",
        "description": "An organization group",
        "attributes": [
            _attribute("displayName", required=True, uniqueness="server"),
            _attribute(
                "members",
                type_="complex",
                multi=True,
                sub=[
                    _attribute("value", mutability="immutable"),
                    _attribute("display", mutability="readOnly"),
                ],
            ),
        ],
        "meta": {"resourceType": "Schema", "location": f"{base_url}/Schemas/{GROUP_SCHEMA}"},
    }
    return [user, group]


def list_response(
    resources: list[dict[str, Any]], *, total: int, start_index: int
) -> dict[str, Any]:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ── request parsing ─────────────────────────────────────────────────────────


def parse_body(raw: bytes) -> dict[str, Any]:
    """The JSON object of a SCIM request body; 400 ``invalidSyntax`` otherwise."""
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        raise bad_request("The request body is not valid JSON", INVALID_SYNTAX) from None
    except RecursionError:
        # A body of thousands of nested ``[`` exhausts the parser's stack.
        raise bad_request("The request body is nested too deeply", INVALID_SYNTAX) from None
    if not isinstance(body, dict):
        raise bad_request("The request body must be a JSON object", INVALID_SYNTAX)
    _refuse_nul(body)
    return body


#: How deeply a request body may nest. SCIM resources need four or five levels.
MAX_BODY_DEPTH = 32


def _refuse_nul(body: object) -> None:
    """No string anywhere in the body may carry a NUL: PostgreSQL refuses it (a 500).

    Walked with an explicit stack, and refusing a body nested deeper than
    :data:`MAX_BODY_DEPTH`, so no body can exhaust the interpreter's stack.
    """
    stack: list[tuple[object, int]] = [(body, 0)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, str):
            if "\x00" in value:
                raise bad_request("Strings must not contain NUL characters")
        elif isinstance(value, dict | list):
            if depth >= MAX_BODY_DEPTH:
                raise bad_request("The request body is nested too deeply", INVALID_SYNTAX)
            items = (
                [item for pair in value.items() for item in pair]
                if isinstance(value, dict)
                else value
            )
            stack.extend((item, depth + 1) for item in items)


def _int_param(params: Mapping[str, str], name: str, default: int) -> int:
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise bad_request(f"{name} must be an integer", INVALID_VALUE) from None


#: The largest ``startIndex`` honoured; beyond it every page is empty anyway, and
#: an unbounded one would overflow the database's 64-bit OFFSET (a 500).
MAX_START_INDEX = 10**9


def paging(params: Mapping[str, str]) -> tuple[int, int]:
    """``(startIndex, count)``: 1-based; count clamped to ``0..MAX_RESULTS`` (RFC 7644 3.4.2.4).

    ``startIndex`` is clamped to ``1..MAX_START_INDEX``.
    """
    start = min(MAX_START_INDEX, max(1, _int_param(params, "startIndex", 1)))
    count = min(MAX_RESULTS, max(0, _int_param(params, "count", DEFAULT_COUNT)))
    return start, count


def excluded_attributes(params: Mapping[str, str]) -> set[str]:
    """Lower-cased names in ``excludedAttributes`` (Azure AD sends ``members``)."""
    raw = params.get("excludedAttributes", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


@dataclass(frozen=True)
class Filter:
    """One ``<attribute> eq "<value>"`` comparison; ``attribute`` is the canonical name."""

    attribute: str
    value: str


_FILTER = re.compile(
    r"^\s*(?P<attr>[A-Za-z][\w.:$-]*(?:\[[^\]]*\])?(?:\.[A-Za-z]\w*)?)\s+(?P<op>[A-Za-z]{2})\s+"
    r'"(?P<value>(?:[^"\\]|\\.)*)"\s*$',
    re.DOTALL,
)
_USER_URN = USER_SCHEMA.lower() + ":"
_GROUP_URN = GROUP_SCHEMA.lower() + ":"

#: Canonical attribute of every accepted spelling, per resource type.
USER_FILTER_ATTRIBUTES = {
    "username": "userName",
    "externalid": "externalId",
    "id": "id",
    "emails.value": "email",
    'emails[type eq "work"].value': "email",
    "emails[primary eq true].value": "email",
}
GROUP_FILTER_ATTRIBUTES = {
    "displayname": "displayName",
    "externalid": "externalId",
    "id": "id",
}


def parse_filter(raw: str | None, allowed: Mapping[str, str]) -> Filter | None:
    """The request's filter, or ``None`` without one; 400 ``invalidFilter`` if unsupported."""
    if raw is None or not raw.strip():
        return None
    if len(raw) > MAX_FILTER_LENGTH or "\x00" in raw:
        raise bad_request("Unsupported filter", INVALID_FILTER)
    match = _FILTER.match(raw)
    if match is None or match["op"].lower() != "eq":
        raise bad_request(
            "Unsupported filter: only one 'attribute eq \"value\"' comparison is supported",
            INVALID_FILTER,
        )
    attr = match["attr"].lower()
    for urn in (_USER_URN, _GROUP_URN):
        attr = attr.removeprefix(urn)
    # ``emails[type eq "work"]`` may carry any spacing inside the brackets.
    attr = re.sub(r"\s+", " ", attr)
    canonical = allowed.get(attr)
    if canonical is None:
        raise bad_request(f"Filtering on '{match['attr']}' is not supported", INVALID_FILTER)
    try:
        value = json.loads(f'"{match["value"]}"')
    except ValueError:
        raise bad_request("The filter value is not a valid string", INVALID_FILTER) from None
    return Filter(attribute=canonical, value=value)


# ── PATCH ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PatchOperation:
    op: str  # add | replace | remove
    #: Lower-cased, the core schema URN prefix removed; ``None`` for a path-less op.
    path: str | None
    #: The path as sent, for error messages.
    raw_path: str | None
    value: Any


def patch_operations(body: Mapping[str, Any]) -> list[PatchOperation]:
    """The ``Operations`` of a PatchOp body, normalised; 400 on a malformed one."""
    operations = body.get("Operations", body.get("operations"))
    if not isinstance(operations, list) or not operations:
        raise bad_request("A PATCH request needs a non-empty 'Operations' list", INVALID_SYNTAX)
    if len(operations) > MAX_OPERATIONS:
        raise bad_request("Too many PATCH operations", INVALID_SYNTAX)
    out: list[PatchOperation] = []
    for item in operations:
        if not isinstance(item, dict):
            raise bad_request("Each PATCH operation must be an object", INVALID_SYNTAX)
        op = str(item.get("op", "")).strip().lower()
        if op not in {"add", "replace", "remove"}:
            raise bad_request(f"Unsupported PATCH op '{item.get('op')}'", INVALID_SYNTAX)
        raw_path = item.get("path")
        if raw_path is not None and not isinstance(raw_path, str):
            raise bad_request("A PATCH path must be a string", INVALID_PATH)
        path = normalize_path(raw_path) if raw_path else None
        if op == "remove" and path is None:
            raise bad_request("A remove operation needs a path", NO_TARGET)
        out.append(PatchOperation(op=op, path=path, raw_path=raw_path, value=item.get("value")))
    return out


def normalize_path(path: str) -> str:
    """Lower-case, collapse spaces, drop the core schema URN prefixes."""
    normalized = re.sub(r"\s+", " ", path.strip()).lower()
    for urn in (_USER_URN, _GROUP_URN):
        normalized = normalized.removeprefix(urn)
    return normalized


def parse_bool(value: object, attribute: str) -> bool:
    """A SCIM boolean; Okta and Azure AD send ``"False"``/``"True"`` strings too."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise bad_request(f"'{attribute}' must be a boolean")


def optional_string(value: object, attribute: str, *, max_length: int) -> str | None:
    """A string attribute, ``None`` for null/empty; 400 for another type or too long."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise bad_request(f"'{attribute}' must be a string")
    stripped = value.strip()
    if len(stripped) > max_length:
        raise bad_request(f"'{attribute}' is longer than {max_length} characters")
    return stripped or None
