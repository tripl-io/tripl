"""The SCIM 2.0 service provider of an organization: ``/scim/v2/{org}`` (F20, GH #273).

Mounted on the app next to ``/api/v1``, not under it, so none of the API's
session machinery applies: the org path rewrite only touches ``/api/v1/orgs/``,
no cookie is read (so there is nothing for a cross-site request to ride), the
hosted email-verification gate and "SSO required" belong to
``api.deps.get_current_user``, which no route here uses. The one gate is
:func:`scim_caller`: a live SCIM token of the organization the path names
(``scim_token_service.authenticate`` — sessions and API keys are 401, a token
of another organization 404, a suspended organization 403), then the token's
own rate-limit bucket (``scim``, 600/min); failed authentications draw on a
per-address bucket (``scim_auth_failure``, 30/min).

Bodies are RFC 7643 JSON read by hand (``application/scim+json`` or
``application/json``), not Pydantic models, and every error is the RFC 7644
§3.12 error body (``tripl.main`` answers :class:`ScimError`). Not in the
OpenAPI document: its shapes are SCIM's, documented in the admin guide.

Every write is audited in the organization as ``org.scim.*`` with no user and
``{"via": "scim", "token_prefix": ...}`` in the payload.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from tripl.api.deps import SessionDep
from tripl.api.v1.auth_sso import app_base_url
from tripl.middleware.org_context import OrgRef, bind_org
from tripl.middleware.rate_limit import (
    allow,
    retry_after_for_key,
    scim_auth_failure_rate_limiter,
    scim_rate_limiter,
)
from tripl.services import (
    audit_service,
    scim_config_service,
    scim_group_service,
    scim_resources,
    scim_token_service,
    scim_user_service,
)
from tripl.services.scim_errors import SCIM_MEDIA_TYPE, ScimError, not_found
from tripl.services.scim_token_service import ScimCaller

router = APIRouter(prefix="/scim/v2/{org}", include_in_schema=False)


async def scim_caller(request: Request, session: SessionDep, org: str) -> ScimCaller:
    """Authenticate the SCIM token, take a rate-limit token, bind the organization.

    A failed authentication (401) draws on the client address's
    ``scim_auth_failure`` bucket; once that is empty the answer is 429.
    """
    try:
        caller = await scim_token_service.authenticate(
            session, org_slug=org, authorization=request.headers.get("Authorization", "")
        )
    except ScimError as exc:
        if exc.status == 401 and not await allow(scim_auth_failure_rate_limiter, request):
            raise ScimError(
                429,
                "Too many failed authentication attempts; please retry shortly.",
                headers={"Retry-After": "60"},
            ) from None
        raise
    retry_after = await retry_after_for_key(scim_rate_limiter, str(caller.token_id))
    if retry_after is not None:
        raise ScimError(
            429,
            "Too many requests; please retry shortly.",
            headers={"Retry-After": str(retry_after)},
        )
    # Fenced by OrgPathRewriteMiddleware, which unbinds it when the request ends.
    bind_org(OrgRef(id=caller.organization_id, slug=caller.organization_slug))
    return caller


CallerDep = Annotated[ScimCaller, Depends(scim_caller)]


def _scim(content: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse(
        content=content, status_code=status_code, media_type=SCIM_MEDIA_TYPE, headers=headers
    )


async def _base_url(session: SessionDep, request: Request, caller: ScimCaller) -> str:
    return scim_config_service.scim_base_url(
        await app_base_url(session, request), caller.organization_slug
    )


async def _body(request: Request) -> dict[str, Any]:
    return scim_resources.parse_body(await request.body())


# ── discovery ───────────────────────────────────────────────────────────────


@router.get("/ServiceProviderConfig")
async def service_provider_config(
    request: Request, session: SessionDep, caller: CallerDep
) -> Response:
    return _scim(scim_resources.service_provider_config(await _base_url(session, request, caller)))


@router.get("/ResourceTypes")
async def resource_types(request: Request, session: SessionDep, caller: CallerDep) -> Response:
    types = scim_resources.resource_types(await _base_url(session, request, caller))
    return _scim(scim_resources.list_response(types, total=len(types), start_index=1))


@router.get("/ResourceTypes/{name}")
async def resource_type(
    request: Request, session: SessionDep, caller: CallerDep, name: str
) -> Response:
    for item in scim_resources.resource_types(await _base_url(session, request, caller)):
        if item["id"] == name:
            return _scim(item)
    raise not_found("Resource type not found")


@router.get("/Schemas")
async def schemas(request: Request, session: SessionDep, caller: CallerDep) -> Response:
    items = scim_resources.schemas(await _base_url(session, request, caller))
    return _scim(scim_resources.list_response(items, total=len(items), start_index=1))


@router.get("/Schemas/{schema_id}")
async def schema(
    request: Request, session: SessionDep, caller: CallerDep, schema_id: str
) -> Response:
    for item in scim_resources.schemas(await _base_url(session, request, caller)):
        if item["id"] == schema_id:
            return _scim(item)
    raise not_found("Schema not found")


# ── users ───────────────────────────────────────────────────────────────────

_USER_ACTIONS = {
    "provision": "org.scim.user_provision",
    "link": "org.scim.user_link",
    "update": "org.scim.user_update",
    "deactivate": "org.scim.user_deactivate",
    "reactivate": "org.scim.user_reactivate",
}


async def _audit_user(
    session: SessionDep, caller: ScimCaller, result: scim_user_service.WriteResult
) -> None:
    await audit_service.record(
        session,
        user=None,
        action=_USER_ACTIONS[result.action],
        target_type="user",
        target_id=result.user.id,
        target_name=result.user.email,
        payload=result.payload,
        organization_id=caller.organization_id,
    )


@router.get("/Users")
async def list_users(request: Request, session: SessionDep, caller: CallerDep) -> Response:
    base = await _base_url(session, request, caller)
    return _scim(
        await scim_user_service.list_users(
            session, caller.organization_id, request.query_params, base
        )
    )


@router.post("/Users")
async def create_user(request: Request, session: SessionDep, caller: CallerDep) -> Response:
    body = await _body(request)
    base = await _base_url(session, request, caller)
    result = await scim_user_service.create_user(session, caller, body, base)
    resource = result.resource
    await _audit_user(session, caller, result)
    return _scim(resource, 201, headers={"Location": resource["meta"]["location"]})


@router.get("/Users/{user_id}")
async def get_user(
    request: Request, session: SessionDep, caller: CallerDep, user_id: str
) -> Response:
    base = await _base_url(session, request, caller)
    return _scim(await scim_user_service.get_user(session, caller.organization_id, user_id, base))


@router.put("/Users/{user_id}")
async def replace_user(
    request: Request, session: SessionDep, caller: CallerDep, user_id: str
) -> Response:
    body = await _body(request)
    base = await _base_url(session, request, caller)
    result = await scim_user_service.replace_user(session, caller, user_id, body, base)
    await _audit_user(session, caller, result)
    return _scim(result.resource)


@router.patch("/Users/{user_id}")
async def patch_user(
    request: Request, session: SessionDep, caller: CallerDep, user_id: str
) -> Response:
    body = await _body(request)
    base = await _base_url(session, request, caller)
    result = await scim_user_service.patch_user(session, caller, user_id, body, base)
    await _audit_user(session, caller, result)
    return _scim(result.resource)


@router.delete("/Users/{user_id}")
async def delete_user(session: SessionDep, caller: CallerDep, user_id: str) -> Response:
    """Deprovision: the membership goes, the account and the link row stay."""
    result = await scim_user_service.delete_user(session, caller, user_id)
    await _audit_user(session, caller, result)
    return Response(status_code=204)


# ── groups ──────────────────────────────────────────────────────────────────

_GROUP_ACTIONS = {
    "create": "org.scim.group_create",
    "update": "org.scim.group_update",
    "delete": "org.scim.group_delete",
}


async def _audit_group(
    session: SessionDep, caller: ScimCaller, result: scim_group_service.GroupWrite
) -> None:
    await audit_service.record(
        session,
        user=None,
        action=_GROUP_ACTIONS[result.action],
        target_type="organization_group",
        target_id=result.group_id,
        target_name=result.name,
        payload=result.payload,
        organization_id=caller.organization_id,
    )


@router.get("/Groups")
async def list_groups(request: Request, session: SessionDep, caller: CallerDep) -> Response:
    base = await _base_url(session, request, caller)
    return _scim(
        await scim_group_service.list_groups(
            session, caller.organization_id, request.query_params, base
        )
    )


@router.post("/Groups")
async def create_group(request: Request, session: SessionDep, caller: CallerDep) -> Response:
    body = await _body(request)
    base = await _base_url(session, request, caller)
    result = await scim_group_service.create_group(session, caller, body, base)
    resource = result.resource
    await _audit_group(session, caller, result)
    return _scim(resource, 201, headers={"Location": resource["meta"]["location"]})


@router.get("/Groups/{group_id}")
async def get_group(
    request: Request, session: SessionDep, caller: CallerDep, group_id: str
) -> Response:
    base = await _base_url(session, request, caller)
    return _scim(
        await scim_group_service.get_group_resource(
            session, caller.organization_id, group_id, request.query_params, base
        )
    )


@router.put("/Groups/{group_id}")
async def replace_group(
    request: Request, session: SessionDep, caller: CallerDep, group_id: str
) -> Response:
    body = await _body(request)
    base = await _base_url(session, request, caller)
    result = await scim_group_service.replace_group(session, caller, group_id, body, base)
    await _audit_group(session, caller, result)
    return _scim(result.resource)


@router.patch("/Groups/{group_id}")
async def patch_group(
    request: Request, session: SessionDep, caller: CallerDep, group_id: str
) -> Response:
    body = await _body(request)
    base = await _base_url(session, request, caller)
    result = await scim_group_service.patch_group(session, caller, group_id, body, base)
    await _audit_group(session, caller, result)
    return _scim(result.resource)


@router.delete("/Groups/{group_id}")
async def delete_group(session: SessionDep, caller: CallerDep, group_id: str) -> Response:
    result = await scim_group_service.delete_group(session, caller, group_id)
    await _audit_group(session, caller, result)
    return Response(status_code=204)


# ── anything else ───────────────────────────────────────────────────────────


@router.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def unknown_endpoint() -> Response:
    """Every other path under the base URL (``/Bulk``, ``/Me``, a typo): a SCIM 404.

    Declared last so every real endpoint matches first; without it such a path
    would fall through to FastAPI's plain 404 (or the SPA). No token is needed
    to learn that an endpoint does not exist. The path parameter is left
    undeclared: the handler never reads it.
    """
    raise not_found("Endpoint not supported")
