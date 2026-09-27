from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.auth_utils import hash_password, hash_session_token, normalize_email
from tripl.models.domain_enums import UserRole
from tripl.models.invitation import Invitation
from tripl.models.user import User
from tripl.services import auth_service

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


async def create_invitation(
    session: AsyncSession,
    *,
    email: str,
    role: UserRole,
    invited_by_user_id: uuid.UUID,
) -> tuple[Invitation, str]:
    """Mint a single-use invitation and return it with its raw token.

    The raw token is returned to the caller ONCE and never stored, so the route
    can put the redeem URL in its response body. That is the primary delivery
    path on purpose: SMTP is optional and unconfigured on many instances, so an
    invite that could only be emailed would not work at all there.

    Refuses an address that already has an account — the owner wants the Members
    screen for that person, not a second identity. Any earlier outstanding
    invite for the same address is dropped so only the newest link works,
    matching how password resets supersede each other.
    """
    normalized = normalize_email(email)

    existing_user = await session.scalar(select(User).where(User.email == normalized))
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That email already has an account. Change their role from Members instead.",
        )

    await session.execute(
        delete(Invitation)
        .where(Invitation.email == normalized, Invitation.used_at.is_(None))
        .execution_options(synchronize_session=False)
    )

    raw_token = secrets.token_urlsafe(INVITATION_TOKEN_BYTES)
    invitation = Invitation(
        email=normalized,
        role=role,
        token_hash=_hash_token(raw_token),
        invited_by_user_id=invited_by_user_id,
        expires_at=_expires_at(),
    )
    session.add(invitation)
    await session.commit()
    await session.refresh(invitation)
    return invitation, raw_token


async def list_pending_invitations(session: AsyncSession) -> list[Invitation]:
    """Outstanding invitations, newest first.

    Expired-but-unused rows are included on purpose: an owner needs to see that
    a link they sent has gone stale, which is exactly when they would re-issue
    it. The redeem path still refuses them.
    """
    rows = await session.scalars(
        select(Invitation)
        .where(Invitation.used_at.is_(None))
        .order_by(Invitation.created_at.desc())
    )
    return list(rows)


async def revoke_invitation(session: AsyncSession, invitation_id: uuid.UUID) -> None:
    """Delete an outstanding invitation, making its link stop working immediately."""
    invitation = await session.get(Invitation, invitation_id)
    if invitation is None:
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
            select(Invitation).where(Invitation.token_hash == _hash_token(raw_token))
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
) -> tuple[User, str]:
    """Consume an invitation and create its account, returning a live session.

    Bypasses ``registration_mode`` by construction rather than by growing that
    check a new arm: the instance-wide door and a named, owner-issued,
    single-use, expiring invitation are different mechanisms, and keeping them
    separate means a bug here can never accidentally widen self-service signup.

    The account is created at the role the OWNER chose when inviting, not at a
    role the invitee can influence, and the address is taken from the invitation
    rather than from the request body — so a link cannot be redeemed into a
    different identity than the one it was issued for.
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
        role=invitation.role,
    )
    session.add(user)
    await session.flush()
    auth_service.add_organization_membership(
        session,
        user,
        organization_id=invitation.organization_id,
        org_role=invitation.org_role,
    )

    invitation.used_at = datetime.now(UTC)
    session_token = await auth_service.create_session_for_user(session, user.id)
    await session.commit()
    await session.refresh(user)
    return user, session_token
