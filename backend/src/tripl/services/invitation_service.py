from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import HTTPException, status
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.auth_utils import hash_password, hash_session_token, normalize_email
from tripl.middleware.org_context import require_org_id
from tripl.models.domain_enums import OrganizationRole
from tripl.models.invitation import Invitation
from tripl.models.organization import Organization, OrganizationMember
from tripl.models.user import User
from tripl.services import auth_service, email_verification_service, org_sso_service
from tripl.services.org_resolution import ORG_IS_ACTIVE

# Long enough that an owner can hand the link over out of band (SMTP is
# optional, so "paste it into Slack" is a first-class path), short enough that a
# forgotten invite is not a standing key to the instance.
INVITATION_TTL_HOURS = 72
# Same generator and width as session and password-reset tokens: 32 bytes via
# ``secrets.token_urlsafe`` is ~256 bits, so the link is not guessable.
INVITATION_TOKEN_BYTES = 32

# Deliberately identical for unknown / expired / already-used tokens so a
# rejected redemption never reveals which of those it hit.
INVALID_INVITATION_MESSAGE = "This invitation link is invalid, expired, or already used."


def _hash_token(raw_token: str) -> str:
    """Digest an invitation token for storage and lookup.

    Reuses the shared keyed-HMAC hasher rather than adding a third token shape:
    only the digest is persisted, so a leaked ``invitations`` table is useless
    without ``SECRET_KEY``.
    """
    return hash_session_token(raw_token)


def _expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(hours=INVITATION_TTL_HOURS)


# Highest first: an invitation "outranks" a role when it grants a role above it.
_ROLE_RANK = {
    OrganizationRole.member.value: 0,
    OrganizationRole.admin.value: 1,
    OrganizationRole.owner.value: 2,
}


async def drop_pending_invitations(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    invited_by_user_id: uuid.UUID,
    email: str | None = None,
    above_role: OrganizationRole | None = None,
) -> int:
    """Delete unused invitations into ``organization_id`` that a member's access backs.

    An invitation link outlives the inviter's standing: it is returned to the
    inviter in the response body and stays redeemable for its whole TTL. So a
    member who is removed, or demoted, could otherwise come back through a link
    they minted earlier.

    * ``invited_by_user_id`` — the invitations this member sent;
    * ``email`` — also those addressed to this member (a removal);
    * ``above_role`` — only those granting a role above it (a demotion: the
      invitations the member could no longer issue). ``None`` drops them all.

    Returns how many were deleted. Does NOT commit.
    """
    condition = Invitation.invited_by_user_id == invited_by_user_id
    if email is not None:
        condition = or_(condition, Invitation.email == normalize_email(email))
    query = delete(Invitation).where(
        Invitation.organization_id == organization_id,
        Invitation.used_at.is_(None),
        condition,
    )
    if above_role is not None:
        floor = _ROLE_RANK[OrganizationRole(above_role).value]
        # A NULL org_role redeems as ``member`` (see :func:`redeem_invitation`).
        query = query.where(
            Invitation.org_role.in_([role for role, rank in _ROLE_RANK.items() if rank > floor])
        )
    result = await session.execute(query.execution_options(synchronize_session=False))
    return int(getattr(result, "rowcount", 0) or 0)


async def create_invitation(
    session: AsyncSession,
    *,
    email: str,
    org_role: OrganizationRole,
    organization_id: uuid.UUID,
    invited_by_user_id: uuid.UUID,
) -> tuple[Invitation, str]:
    """Mint a single-use invitation into ``organization_id`` and return it with its raw token.

    The invitee joins that organization at ``org_role``; the legacy
    ``invitations.role`` column keeps its default and is not read.

    The raw token is returned to the caller ONCE and never stored, so the route
    can put the redeem URL in its response body. That is the primary delivery
    path on purpose: SMTP is optional and unconfigured on many instances, so an
    invite that could only be emailed would not work at all there.

    An address that already has an account may be invited into an organization
    it is not in yet (F20 PR6): that person accepts while signed in and gains a
    membership (:func:`accept_as_signed_in`). Refused with 409 when the account
    is already a member — the owner wants the Members screen for that person.
    Any earlier outstanding invite for the same address into the same
    organization is dropped so only the newest link works, matching how
    password resets supersede each other.
    """
    normalized = normalize_email(email)

    existing_member: uuid.UUID | None = await session.scalar(
        select(OrganizationMember.id)
        .join(User, User.id == OrganizationMember.user_id)
        .where(User.email == normalized, OrganizationMember.organization_id == organization_id)
    )
    if existing_member is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "That email already has an account in this organization. "
                "Change their role from Members instead."
            ),
        )

    await session.execute(
        delete(Invitation)
        .where(
            Invitation.email == normalized,
            Invitation.organization_id == organization_id,
            Invitation.used_at.is_(None),
        )
        .execution_options(synchronize_session=False)
    )

    raw_token = secrets.token_urlsafe(INVITATION_TOKEN_BYTES)
    invitation = Invitation(
        email=normalized,
        org_role=OrganizationRole(org_role).value,
        organization_id=organization_id,
        token_hash=_hash_token(raw_token),
        invited_by_user_id=invited_by_user_id,
        expires_at=_expires_at(),
    )
    session.add(invitation)
    await session.commit()
    await session.refresh(invitation)
    return invitation, raw_token


async def list_pending_invitations(
    session: AsyncSession, organization_id: uuid.UUID | None = None
) -> list[Invitation]:
    """Outstanding invitations into ``organization_id`` (default: the bound one), newest first.

    Expired-but-unused rows are included on purpose: an owner needs to see that
    a link they sent has gone stale, which is exactly when they would re-issue
    it. The redeem path still refuses them.
    """
    org_id = organization_id if organization_id is not None else require_org_id()
    rows = await session.scalars(
        select(Invitation)
        .where(Invitation.used_at.is_(None), Invitation.organization_id == org_id)
        .order_by(Invitation.created_at.desc())
    )
    return list(rows)


async def revoke_invitation(
    session: AsyncSession, invitation_id: uuid.UUID, organization_id: uuid.UUID | None = None
) -> None:
    """Delete an outstanding invitation, making its link stop working immediately.

    Only an invitation into ``organization_id`` (default: the bound one); any
    other is the same 404 as an unknown id.
    """
    org_id = organization_id if organization_id is not None else require_org_id()
    invitation = await session.get(Invitation, invitation_id)
    if invitation is None or invitation.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    await session.delete(invitation)
    await session.commit()


async def get_valid_invitation(session: AsyncSession, raw_token: str) -> Invitation:
    """Resolve a redeemable invitation, or raise the single neutral 400.

    Unknown, expired and already-used tokens are indistinguishable to the caller.
    """
    invitation = cast(
        Invitation | None,
        await session.scalar(
            select(Invitation)
            .join(Organization, Organization.id == Invitation.organization_id)
            # An invitation into an organization being deleted is dead (F20 PR6).
            .where(Invitation.token_hash == _hash_token(raw_token), ORG_IS_ACTIVE)
        ),
    )
    if (
        invitation is None
        or invitation.used_at is not None
        or invitation.expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_INVITATION_MESSAGE,
        )
    return invitation


async def redeem_invitation(
    session: AsyncSession,
    *,
    raw_token: str,
    password: str,
    name: str | None,
) -> tuple[User, str, Invitation]:
    """Consume an invitation and create its account, returning a live session.

    Bypasses ``registration_mode`` by construction rather than by growing that
    check a new arm: the instance-wide door and a named, owner-issued,
    single-use, expiring invitation are different mechanisms, and keeping them
    separate means a bug here can never accidentally widen self-service signup.

    The account joins the invitation's organization at the organization role the
    inviter chose (``invitations.org_role``), not at a role the invitee can
    influence, and the address is taken from the invitation
    rather than from the request body — so a link cannot be redeemed into a
    different identity than the one it was issued for.

    Self-hosted, the new account is email-verified at creation, as every
    self-hosted account is. Hosted, it starts UNVERIFIED: the inviter receives
    the raw link in the response body, so redeeming it proves nothing about
    who reads the address; the caller mails a verification link.
    """
    invitation = await get_valid_invitation(session, raw_token)

    # Re-checked here rather than trusted from mint time: an address can acquire
    # an account between minting and redeeming (the instance may be in "open"
    # mode, or the person may have registered themselves meanwhile).
    existing_user = await session.scalar(select(User).where(User.email == invitation.email))
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account already exists for this email. Sign in instead.",
        )

    stripped_name = (name or "").strip() or None
    user = User(
        email=invitation.email,
        name=stripped_name,
        password_hash=await asyncio.to_thread(hash_password, password),
    )
    if not email_verification_service.verification_required():
        email_verification_service.mark_verified(user)
    session.add(user)
    await session.flush()
    auth_service.add_organization_membership(
        session,
        user,
        organization_id=invitation.organization_id,
        # Migration c9e1a3b5d7f9 filled every pending invitation's org_role; a
        # NULL could only come from a row written behind the application's
        # back, and gets the least privilege.
        org_role=OrganizationRole(invitation.org_role or OrganizationRole.member.value),
    )

    invitation.used_at = datetime.now(UTC)
    session_token = await auth_service.create_session_for_user(session, user.id)
    await session.commit()
    await session.refresh(user)
    return user, session_token, invitation


class InvitationEmailMismatchError(Exception):
    """The signed-in account is not the one the invitation was sent to."""


class AlreadyMemberError(Exception):
    """The signed-in account already belongs to the invitation's organization."""


class EmailNotVerifiedError(Exception):
    """A hosted instance's signed-in account has not verified its address yet."""


async def accept_as_signed_in(session: AsyncSession, *, raw_token: str, user: User) -> Invitation:
    """Redeem an invitation for an EXISTING, signed-in account: add the membership.

    Only when the account's email is the invitation's (both normalized, so the
    comparison is case-insensitive); anything else raises
    :class:`InvitationEmailMismatchError` and the invitation stays unused. On a
    hosted instance the account must also have verified that address
    (:class:`EmailNotVerifiedError`, critique #27): anyone may sign up with any
    address there, so the match alone proves nothing. The role is the one the
    inviter chose. Does NOT commit: the caller files its audit row and commits
    once.
    """
    invitation = await get_valid_invitation(session, raw_token)
    if normalize_email(user.email) != normalize_email(invitation.email):
        raise InvitationEmailMismatchError
    if email_verification_service.is_blocked(user):
        raise EmailNotVerifiedError
    member_id: uuid.UUID | None = await session.scalar(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == invitation.organization_id,
            OrganizationMember.user_id == user.id,
        )
    )
    if member_id is not None:
        raise AlreadyMemberError
    auth_service.add_organization_membership(
        session,
        user,
        organization_id=invitation.organization_id,
        org_role=OrganizationRole(invitation.org_role or OrganizationRole.member.value),
    )
    # Invited back after a removal: an SSO sign-in may add them again (F20).
    await org_sso_service.lift_membership_block(session, invitation.organization_id, user.id)
    invitation.used_at = datetime.now(UTC)
    await session.flush()
    return invitation
