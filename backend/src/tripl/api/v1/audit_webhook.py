"""An organization's audit webhook: ``/api/v1/orgs/{org}/audit/webhook`` (F20, GH #273).

Reached through the org-qualified rewrite (``audit`` is an org rewrite
prefix), so the organization is the request's bound one. Every route is for an
OWNER of it, from a browser session (``get_org_owner_user``): an admin, a
member and any API key get 403, a stranger 404. Owners only, reads included:
where the audit log is copied to is a security decision.

* ``GET/PUT/DELETE /audit/webhook`` — the settings. The secret is generated
  on create and shown once; after that only ``secret_configured``.
* ``POST /audit/webhook/rotate-secret`` — a new secret, shown once.
* ``POST /audit/webhook/test`` — send a synthetic ``audit.webhook_test`` event
  now and report the answer.

``PUT`` (a DNS lookup on a hosted instance) and ``/test`` (an outbound
request) share the ``audit_webhook_probe`` rate-limit bucket.
* ``GET /audit/webhook/deliveries`` — the recent outbox rows.

Every change is audited as ``org.audit_webhook.*`` with the URL's host only,
never the secret or the full URL (which may carry a token in its path).
Included BEFORE the audit router, whose ``/audit/{entry_id}`` would otherwise
claim ``/audit/webhook``.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Response, status

from tripl.api.deps import OrgOwnerUserDep, SessionDep
from tripl.middleware.org_context import current_org, require_org_id
from tripl.middleware.rate_limit import audit_webhook_probe_rate_limiter, enforce
from tripl.schemas.audit_webhook import (
    AuditWebhookDeliveryResponse,
    AuditWebhookDeliveryStatus,
    AuditWebhookResponse,
    AuditWebhookSaved,
    AuditWebhookTestResult,
    AuditWebhookUpdate,
)
from tripl.services import audit_service, audit_webhook_service

router = APIRouter(prefix="/audit/webhook", tags=["audit"])


def _host(url: str) -> str:
    """What the audit row keeps of the URL: a webhook URL may carry a token in its path."""
    return urlparse(url).hostname or ""


def _org_slug() -> str:
    org = current_org()
    return org.slug if org is not None else ""


@router.get("", response_model=AuditWebhookResponse)
async def get_audit_webhook(
    session: SessionDep, current_user: OrgOwnerUserDep
) -> AuditWebhookResponse:
    del current_user
    hook = await audit_webhook_service.get_webhook(session, require_org_id())
    return audit_webhook_service.webhook_response(hook)


@router.put(
    "",
    response_model=AuditWebhookSaved,
    dependencies=[Depends(enforce(audit_webhook_probe_rate_limiter))],
)
async def put_audit_webhook(
    session: SessionDep, data: AuditWebhookUpdate, current_user: OrgOwnerUserDep
) -> AuditWebhookSaved:
    """Create the webhook (the answer carries its secret, once) or change it.

    422 for a URL that is not https, carries credentials, or (hosted) names a
    private host.
    """
    org_id = require_org_id()
    saved = await audit_webhook_service.save_webhook(session, org_id, data)
    response = audit_webhook_service.saved_response(saved.hook, saved.secret)
    await audit_service.record(
        session,
        user=current_user,
        action="org.audit_webhook.create" if saved.created else "org.audit_webhook.update",
        target_type="organization",
        target_id=org_id,
        target_name=_org_slug(),
        payload={
            "host": _host(saved.hook.url),
            "enabled": saved.hook.enabled,
            "changed": saved.changed,
        },
    )
    return response


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_audit_webhook(session: SessionDep, current_user: OrgOwnerUserDep) -> Response:
    org_id = require_org_id()
    hook = await audit_webhook_service.require_webhook(session, org_id)
    host = _host(hook.url)
    dropped = await audit_webhook_service.delete_webhook(session, hook)
    await audit_service.record(
        session,
        user=current_user,
        action="org.audit_webhook.delete",
        target_type="organization",
        target_id=org_id,
        target_name=_org_slug(),
        payload={"host": host, "undelivered_dropped": dropped},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/rotate-secret", response_model=AuditWebhookSaved)
async def rotate_audit_webhook_secret(
    session: SessionDep, current_user: OrgOwnerUserDep
) -> AuditWebhookSaved:
    """A new signing secret, shown once; the old one stops working at once."""
    org_id = require_org_id()
    hook = await audit_webhook_service.require_webhook(session, org_id)
    secret = await audit_webhook_service.rotate_secret(session, hook)
    response = audit_webhook_service.saved_response(hook, secret)
    await audit_service.record(
        session,
        user=current_user,
        action="org.audit_webhook.rotate_secret",
        target_type="organization",
        target_id=org_id,
        target_name=_org_slug(),
        payload={"host": _host(hook.url)},
    )
    return response


@router.post(
    "/test",
    response_model=AuditWebhookTestResult,
    dependencies=[Depends(enforce(audit_webhook_probe_rate_limiter))],
)
async def send_audit_webhook_test(
    session: SessionDep, current_user: OrgOwnerUserDep
) -> AuditWebhookTestResult:
    """Send a synthetic ``audit.webhook_test`` event now; 200 whatever the receiver said."""
    org_id = require_org_id()
    hook = await audit_webhook_service.require_webhook(session, org_id)
    result = await audit_webhook_service.send_test(
        hook, org_slug=_org_slug(), user_email=current_user.email
    )
    await audit_service.record(
        session,
        user=current_user,
        action="org.audit_webhook.test",
        target_type="organization",
        target_id=org_id,
        target_name=_org_slug(),
        payload={"ok": result.ok, "status_code": result.status_code, "error": result.error},
    )
    return result


@router.get("/deliveries", response_model=list[AuditWebhookDeliveryResponse])
async def list_audit_webhook_deliveries(
    session: SessionDep,
    current_user: OrgOwnerUserDep,
    status_filter: Annotated[AuditWebhookDeliveryStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AuditWebhookDeliveryResponse]:
    del current_user
    return await audit_webhook_service.list_deliveries(
        session, require_org_id(), status_filter=status_filter, limit=limit
    )
