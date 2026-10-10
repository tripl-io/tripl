"""A password reset link handed over by hand instead of mailed.

Without a mail relay the emailed reset (``POST /auth/password-reset/request``)
issues nothing, so someone who forgot their password needs a person to give
them a link: an owner or admin of their organization, from Settings ›
Organization › Members (:func:`issue_for_member`), or, when nobody can sign in,
the server's operator with ``tripl-admin password-reset-link``
(:func:`issue_from_shell`). Both mint the emailed reset's own token
(``auth_service.stage_password_reset_token``): it works once, lasts as long
as a mailed one, and replaces any earlier link of the account. Redeeming it is
the ordinary ``POST /auth/password-reset/confirm``, which signs the account
out everywhere and revokes its API keys. Both file ``user.password_reset_link``
in the same commit as the token, and the raw token is never in the audit row.

Whoever holds the link can take the account over, so who may mint one for
whom is most of this module. From the Members page:

* an owner or admin, for another member of the organization, under the role
  rules: only an owner may for an owner;
* only when the caller manages the account in every organization it belongs
  to, by the same rule: a membership elsewhere is someone else's to vouch for;
* for a platform admin, only another platform admin;
* never for an account that has not verified its address: redeeming the link
  records the address as verified, and a link handed over proves nothing about
  the address (self-hosted accounts are verified when they are created);
* never for oneself, so a signed-in session cannot turn itself into the
  account's password.

From the shell, any account: shell access is database access already, and the
operator vouches for the address, as for a ``grant-platform-admin``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl.models.domain_enums import OrganizationRole
from tripl.models.organization import OrganizationMember
from tripl.models.user import User
from tripl.services import audit_service, auth_service, platform_admins, user_service

OWN_ACCOUNT = (
    "You can't create a password reset link for your own account. Ask another owner or admin."
)
ANOTHER_ORGANIZATION = (
    "This account also belongs to an organization you don't manage, so you can't create a "
    "password reset link for it."
)
PLATFORM_ADMIN = "Only a platform admin can create a password reset link for a platform admin."
UNVERIFIED = (
    "This account has not verified its email address, so its password can only be reset by email."
)


class ResetLinkForbiddenError(Exception):
    """The caller may not hand this account a reset link; the message says why."""


class UnverifiedAccountError(Exception):
    """The account has not verified its address, so no link is handed over for it."""


@dataclass(frozen=True)
class IssuedResetLink:
    """A minted link: whose it is, its path on the app's URL, and when it stops working."""

    user: User
    path: str
    expires_at: datetime


def _manages(actor_role: str | None, target_role: str) -> bool:
    """Whether ``actor_role`` may act on a member holding ``target_role``, in one organization."""
    owner = OrganizationRole.owner.value
    if actor_role == owner:
        return True
    return actor_role == OrganizationRole.admin.value and target_role != owner


async def issue_for_member(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, *, actor: User
) -> IssuedResetLink:
    """Mint a reset link for ``user_id``, a member of ``org_id``, on ``actor``'s word. Commits.

    Raises :class:`LookupError` when ``user_id`` is not a member of ``org_id``,
    ``user_service.OwnerManagementError`` when the actor does not manage them
    there (no longer an owner or admin, or an admin and the member an owner),
    :class:`ResetLinkForbiddenError` for the actor's own account, a member of an
    organization the actor does not manage, or a platform admin when the actor
    is not one, and :class:`UnverifiedAccountError` for an unverified address.

    The actor's role is read under the organization's owner-set lock, as for a
    role change (``user_service.update_org_role``): an owner demoted meanwhile
    cannot still act on owners.
    """
    await auth_service.acquire_owner_set_xact_lock(session, org_id)
    rows = await session.execute(
        select(
            OrganizationMember.user_id,
            OrganizationMember.organization_id,
            OrganizationMember.role,
        ).where(OrganizationMember.user_id.in_((actor.id, user_id)))
    )
    actor_roles: dict[uuid.UUID, str] = {}
    target_roles: dict[uuid.UUID, str] = {}
    for member_id, member_org_id, role in rows.all():
        if member_id == user_id:
            target_roles[member_org_id] = str(role)
        if member_id == actor.id:
            actor_roles[member_org_id] = str(role)

    target_role = target_roles.get(org_id)
    if target_role is None:
        raise LookupError(user_id)
    if user_id == actor.id:
        raise ResetLinkForbiddenError(OWN_ACCOUNT)
    if not _manages(actor_roles.get(org_id), target_role):
        raise user_service.OwnerManagementError
    if any(
        not _manages(actor_roles.get(other_org_id), role)
        for other_org_id, role in target_roles.items()
        if other_org_id != org_id
    ):
        raise ResetLinkForbiddenError(ANOTHER_ORGANIZATION)

    target = await session.get(User, user_id)
    if target is None:  # pragma: no cover - the membership row references the user
        raise LookupError(user_id)
    if target.is_platform_admin and not actor.is_platform_admin:
        raise ResetLinkForbiddenError(PLATFORM_ADMIN)
    if target.email_verified_at is None:
        raise UnverifiedAccountError(UNVERIFIED)

    raw_token, expires_at = await session.run_sync(
        auth_service.stage_password_reset_token, target.id
    )
    await audit_service.record(
        session,
        user=actor,
        action="user.password_reset_link",
        target_type="user",
        target_id=target.id,
        target_name=target.email,
        payload={"role": target_role},
        organization_id=org_id,
    )
    return IssuedResetLink(
        user=target, path=auth_service.password_reset_path(raw_token), expires_at=expires_at
    )


def issue_from_shell(session: Session, email: str) -> IssuedResetLink:
    """Mint a reset link for the account at ``email``, for the server's operator. Commits.

    ``email`` is already normalized. Raises :class:`LookupError` when no account
    has it. The audit row is at platform scope with no user (the shell is not
    an account) and ``"via": "tripl-admin"``, like the platform-admin grants.
    """
    target: User | None = session.scalar(select(User).where(User.email == email))
    if target is None:
        raise LookupError(email)
    raw_token, expires_at = auth_service.stage_password_reset_token(session, target.id)
    session.add(
        platform_admins.platform_audit_row(
            target,
            action="user.password_reset_link",
            actor=None,
            payload={"email": target.email, "via": "tripl-admin"},
        )
    )
    session.commit()
    session.refresh(target)
    return IssuedResetLink(
        user=target, path=auth_service.password_reset_path(raw_token), expires_at=expires_at
    )
