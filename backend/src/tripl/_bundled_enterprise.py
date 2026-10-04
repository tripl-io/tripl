"""The enterprise features still in this repository, wired through the extension hooks.

Single sign-on, SCIM provisioning and the audit webhook (F20) are registered
as a bundled :class:`~tripl.extensions.Extension`, so the core reaches them only
through :mod:`tripl.extensions`. Moving them to the ``tripl-enterprise`` package
then moves this module with them and changes nothing in the core.

The API routers are imported when first asked for, so a Celery worker loading
the extension for its tasks does not import the HTTP layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, update

from tripl.extensions import ErrorKind, Extension, ExtensionRouter
from tripl.models.org_scim import OrgScimConfig, OrgScimToken, ScimUserLink
from tripl.services import (
    audit_webhook_outbox,
    audit_webhook_service,
    org_sso_service,
    scim_group_service,
    scim_role_sync,
    scim_token_service,
    scim_user_service,
    sso_gate,
)
from tripl.services.scim_errors import INVALID_SYNTAX, SCIM_PATH_PREFIX, ScimError
from tripl.services.scim_errors import error_response as scim_error_response

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from sqlalchemy.ext.asyncio import AsyncSession
    from starlette.responses import Response

    from tripl.middleware.org_context import OrgRef
    from tripl.models.audit_log import AuditLog
    from tripl.models.domain_enums import OrganizationRole
    from tripl.models.user import User


class _Bundled(Extension):
    name = "bundled-enterprise"

    # -- routes and app --------------------------------------------------
    def api_routers(self) -> Sequence[ExtensionRouter]:
        from tripl.api.v1.audit_webhook import router as audit_webhook_router
        from tripl.api.v1.auth_sso import router as auth_sso_router
        from tripl.api.v1.org_scim import router as org_scim_router
        from tripl.api.v1.org_sso import router as org_sso_router

        return (
            # Signing in through an organization's identity provider: unauthenticated.
            ExtensionRouter(auth_sso_router, protected=False),
            ExtensionRouter(audit_webhook_router, outbound="send audit events to a webhook"),
            # An organization's single sign-on settings: owners of that organization.
            ExtensionRouter(org_sso_router, outbound="configure single sign-on"),
            # Its SCIM tokens and admin-group mapping: owners of that organization.
            ExtensionRouter(org_scim_router, outbound="provision users over SCIM"),
        )

    def install_app(self, app: FastAPI) -> None:
        from tripl.api.scim import router as scim_router

        # SCIM 2.0 provisioning: ``/scim/v2/{org}``, beside ``/api/v1`` rather
        # than under it — identity providers expect a SCIM base URL of their
        # own, and none of the API's session gates apply (``tripl.api.scim``).
        app.include_router(scim_router)

        async def scim_error_handler(request: Request, exc: Exception) -> Response:
            """A SCIM request's error, in the RFC 7644 §3.12 error format."""
            assert isinstance(exc, ScimError)
            return scim_error_response(exc)

        app.add_exception_handler(ScimError, scim_error_handler)

    def error_response(
        self,
        path: str,
        kind: ErrorKind,
        status_code: int,
        detail: str,
        headers: Mapping[str, str] | None = None,
    ) -> Response | None:
        # A SCIM client reads every error in the RFC 7644 §3.12 format.
        if not path.startswith(SCIM_PATH_PREFIX):
            return None
        if kind == "validation":
            return scim_error_response(ScimError(400, detail, scim_type=INVALID_SYNTAX))
        return scim_error_response(ScimError(status_code, detail, headers=dict(headers or {})))

    # -- access gates ----------------------------------------------------
    async def org_session_gate(
        self,
        request: Request,
        session: AsyncSession,
        user: User,
        org: OrgRef,
        *,
        role: OrganizationRole | None = None,
    ) -> None:
        await sso_gate.refuse_non_sso_session(request, session, user, org, role=role)

    async def api_key_use_gate(
        self,
        request: Request,
        session: AsyncSession,
        user: User,
        org_id: uuid.UUID,
        api_key: object,
    ) -> None:
        await sso_gate.refuse_key_without_sso(request, session, user, org_id, api_key)

    async def api_key_mint_gate(self, session: AsyncSession, org: OrgRef) -> None:
        await sso_gate.refuse_key_mint(session, org)

    # -- lifecycle -------------------------------------------------------
    async def on_member_removed(
        self, session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, *, by_admin: bool
    ) -> Mapping[str, int]:
        # Their SCIM tokens are an owner's credential, and they are no member now.
        scim_tokens = await scim_token_service.revoke_tokens_created_by(session, org_id, user_id)
        # Their IdP identity no longer signs them in here, and signing in through
        # the provider again does not re-add them until they accept a new
        # invitation (an SSO membership block).
        identities = await org_sso_service.drop_identities(session, org_id, user_id)
        if by_admin:
            # A removal by an owner or admin is not the IdP's to undo: SCIM shows
            # the user inactive and refuses to re-activate them until they are
            # back in through an invitation (``scim_user_service``).
            await session.execute(
                update(ScimUserLink)
                .where(ScimUserLink.organization_id == org_id, ScimUserLink.user_id == user_id)
                .values(active=False, removed_outside_scim=True)
                .execution_options(synchronize_session=False)
            )
        return {"sso_identities": identities, "scim_tokens": scim_tokens}

    async def on_owner_demoted(
        self, session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        # A SCIM token is an owner's credential; a former owner keeps none.
        await scim_token_service.revoke_tokens_created_by(session, org_id, user_id)

    async def on_membership_restored(
        self, session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        await org_sso_service.lift_membership_block(session, org_id, user_id)

    async def on_org_deleting(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        # SCIM's config and group links reference the organization's groups.
        await scim_group_service.delete_org_scim_groups(session, org_id)
        await scim_user_service.delete_org_scim_users(session, org_id)
        await session.execute(delete(OrgScimConfig).where(OrgScimConfig.organization_id == org_id))
        await session.execute(delete(OrgScimToken).where(OrgScimToken.organization_id == org_id))
        await org_sso_service.delete_org_sso(session, org_id)
        await audit_webhook_service.delete_org_webhook(session, org_id)

    async def on_group_change(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        group_id: uuid.UUID,
        *,
        added: Sequence[uuid.UUID] = (),
        removed: Sequence[uuid.UUID] = (),
    ) -> None:
        # The SCIM admin-group mapping, whoever changes the group.
        await scim_role_sync.on_group_change(
            session, org_id, group_id, added=added, removed=removed
        )

    # -- audit -----------------------------------------------------------
    async def on_audit_recorded(
        self, session: AsyncSession, entry: AuditLog, org_id: uuid.UUID
    ) -> None:
        # The organization's audit webhook, in this very transaction: the row is
        # delivered if and only if it commits (audit_webhook_outbox).
        await audit_webhook_outbox.enqueue(session, entry, org_id)

    # -- worker ----------------------------------------------------------
    def celery_task_modules(self) -> Sequence[str]:
        return ("tripl.worker.tasks.audit_webhook",)

    def beat_schedule(self) -> Mapping[str, Mapping[str, Any]]:
        return {
            "deliver-audit-webhooks": {
                "task": "tripl.worker.tasks.audit_webhook.deliver_audit_webhooks",
                # Every 30 seconds: the one entry off the crontab grid. An audit
                # webhook feeds a SIEM, where a minute of lag is visible, and the
                # tick is one indexed read of due outbox rows when nothing is
                # queued. A tick not started within its interval is dropped; the
                # next one covers it.
                "schedule": timedelta(seconds=30),
                "options": {"expires": 30},
            },
        }


extension = _Bundled()
