from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import HTTPException, status
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import extensions, tenancy
from tripl.auth_utils import hash_password, hash_session_token, normalize_email
from tripl.middleware.org_context import require_org_id
from tripl.models.audit_log import AuditLog
from tripl.models.domain_enums import OrganizationRole
from tripl.models.invitation import Invitation
from tripl.models.organization import Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.services import audit_service, auth_service, email_verification_service
from tripl.services.org_resolution import ORG_IS_ACTIVE

# Long enough that an owner can hand the link over out of band (SMTP is
# optional, so "paste it into Slack" is a first-class path), short enough that a
# forgotten invite is not a standing key to the instance.
INVITATION_TTL_HOURS = 72
# Same generator and width as session and password-reset tokens: 32 bytes via
# ``secrets.token_urlsafe`` is ~256 bits, so the link is not guessable.
INVITATION_TOKEN_BYTES = 32
DEMO_ORGANIZATION_CAPACITY = 10
DEMO_INVITATIONS_PER_HOUR = 10
# The audit action every mint files (:func:`create_invitation`, its one writer).
# The public-demo hourly quota counts these rows, see
# :func:`_check_demo_invitation_limits` for why the audit log is that ledger.
INVITE_AUDIT_ACTION = "user.invite"
# Tag of the per-inviter mint-quota advisory lock: its own key space, so an
# inviter's key can never be an organization's owner-set key.
_DEMO_MINT_LOCK_TAG = b"trplinv1"

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

    The invitee joins that organization at ``org_role``.

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
    if tenancy.public_demo():
        if OrganizationRole(org_role) != OrganizationRole.member:
            raise HTTPException(
                status_code=403, detail="Public demo invitations allow only members."
            )
        await acquire_demo_mint_locks(session, organization_id, invited_by_user_id)

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

    if tenancy.public_demo():
        await _check_demo_invitation_limits(
            session, organization_id, invited_by_user_id, normalized
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
    await session.flush()
    # The one place a mint is audited, in the mint's own transaction: the
    # public-demo quota counts these rows under the locks taken above, so the
    # row must be durable exactly when the invitation is.
    payload: dict[str, object] = {"role": invitation.org_role}
    if tenancy.public_demo():
        payload["public_demo"] = True
    await audit_service.record(
        session,
        user=await session.get(User, invited_by_user_id),
        action=INVITE_AUDIT_ACTION,
        target_type="invitation",
        target_id=invitation.id,
        target_name=invitation.email,
        payload=payload,
        organization_id=organization_id,
        commit=False,
    )
    await session.commit()
    await session.refresh(invitation)
    return invitation, raw_token


def demo_mint_lock_key(inviter_id: uuid.UUID) -> int:
    """The signed 64-bit advisory-lock key guarding ``inviter_id``'s mint quota."""
    digest = hashlib.blake2b(_DEMO_MINT_LOCK_TAG + inviter_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def acquire_demo_mint_locks(
    session: AsyncSession, organization_id: uuid.UUID, inviter_id: uuid.UUID
) -> None:
    """Serialise public-demo mints on both quota dimensions, in the global lock order.

    The quota is per organization AND per inviter (an inviter can mint into
    several organizations), so a mint holds two locks until it commits:

    1. the organization's owner-set lock
       (:func:`auth_service.acquire_owner_set_xact_lock`), which acceptance
       (:func:`accept_as_signed_in`) also takes, so capacity is checked and
       consumed under one lock;
    2. then the inviter's mint lock (:func:`demo_mint_lock_key`, its own key
       space). It is a leaf: nothing is acquired while it is held.

    Always in that order. Every other path holds at most one owner-set lock and
    no mint lock, so two mints, or a mint and any owner-set change, can wait on
    each other but never in a cycle. See the lock-order note on
    :func:`auth_service.acquire_owner_set_xact_lock`.

    PostgreSQL-only like the owner-set lock: a no-op on SQLite (tests).
    """
    await auth_service.acquire_owner_set_xact_lock(session, organization_id)
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": demo_mint_lock_key(inviter_id)}
    )


async def _check_demo_invitation_limits(
    session: AsyncSession, org_id: uuid.UUID, inviter_id: uuid.UUID, email: str
) -> None:
    """DB-backed rolling mint quotas survive replacement, revocation and restarts.

    The quota counts mints, and the ledger of mints is the ``user.invite`` audit
    trail (:data:`INVITE_AUDIT_ACTION`), written by :func:`create_invitation` in
    the mint's transaction. The ``invitations`` table cannot be that ledger:
    a revocation and a re-invite of the same address hard-delete their rows, so
    counting it would let revoke-and-mint loops through without limit. Audit
    rows are append-only and outlive the invitation. Callers hold
    :func:`acquire_demo_mint_locks`, so the count and the row it guards cannot
    interleave with a concurrent mint on either dimension.
    """
    now = datetime.now(UTC)
    for dimension in (AuditLog.organization_id == org_id, AuditLog.user_id == inviter_id):
        count = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == INVITE_AUDIT_ACTION,
                AuditLog.created_at >= now - timedelta(hours=1),
                dimension,
            )
        )
        if (count or 0) >= DEMO_INVITATIONS_PER_HOUR:
            raise HTTPException(
                status_code=429, detail="Public demo invitation limit: 10 per hour."
            )
    members = await session.scalar(
        select(func.count())
        .select_from(OrganizationMember)
        .where(
            OrganizationMember.organization_id == org_id,
        )
    )
    pending = await session.scalar(
        select(func.count())
        .select_from(Invitation)
        .where(
            Invitation.organization_id == org_id,
            Invitation.used_at.is_(None),
            Invitation.expires_at > now,
            Invitation.email != email,
        )
    )
    if (members or 0) + (pending or 0) >= DEMO_ORGANIZATION_CAPACITY:
        raise HTTPException(
            status_code=409,
            detail="Public demo organizations allow 10 members and pending invitations.",
        )


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
            .execution_options(populate_existing=True)
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
    if tenancy.public_demo():
        raise HTTPException(
            status_code=403, detail="Sign in with Google before accepting a demo invitation."
        )
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
        org_role=OrganizationRole(invitation.org_role),
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
    if tenancy.public_demo():
        await auth_service.acquire_owner_set_xact_lock(session, invitation.organization_id)
        # Another acceptance/revocation may have completed while waiting for the lock.
        invitation = await get_valid_invitation(session, raw_token)
    if normalize_email(user.email) != normalize_email(invitation.email):
        raise InvitationEmailMismatchError
    if email_verification_service.is_blocked(user) or (
        tenancy.public_demo() and user.email_verified_at is None
    ):
        raise EmailNotVerifiedError
    member_id: uuid.UUID | None = await session.scalar(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == invitation.organization_id,
            OrganizationMember.user_id == user.id,
        )
    )
    if member_id is not None:
        raise AlreadyMemberError
    if tenancy.public_demo():
        members = await session.scalar(
            select(func.count())
            .select_from(OrganizationMember)
            .where(
                OrganizationMember.organization_id == invitation.organization_id,
            )
        )
        if (members or 0) >= DEMO_ORGANIZATION_CAPACITY:
            raise HTTPException(status_code=409, detail="Public demo organization is full.")
        # Invitations minted before PUBLIC_DEMO was enabled cannot elevate access.
        invitation.org_role = OrganizationRole.member.value
    auth_service.add_organization_membership(
        session,
        user,
        organization_id=invitation.organization_id,
        org_role=OrganizationRole(invitation.org_role),
    )
    # Invited back after a removal: an SSO sign-in may add them again (F20).
    await extensions.on_membership_restored(session, invitation.organization_id, user.id)
    if tenancy.public_demo():
        await _grant_demo_project_access(session, invitation, user)
    invitation.used_at = datetime.now(UTC)
    await session.flush()
    return invitation


async def _grant_demo_project_access(
    session: AsyncSession, invitation: Invitation, user: User
) -> None:
    """Grant only ready demos in the invited org; existing project roles win.

    The demos that become ready LATER are shared by
    :func:`share_demo_with_colleagues`, so between them a colleague sees every
    demo of the organization whatever the order of acceptance and generation.
    """
    projects = await session.scalars(
        select(Project.id).where(
            Project.organization_id == invitation.organization_id,
            Project.is_demo.is_(True),
            Project.generation_status == "ready",
            ~select(ProjectMember.id)
            .where(
                ProjectMember.project_id == Project.id,
                ProjectMember.user_id == user.id,
            )
            .exists(),
        )
    )
    session.add_all(
        [
            ProjectMember(
                project_id=project_id,
                user_id=user.id,
                role="viewer",
                added_by_user_id=invitation.invited_by_user_id,
            )
            for project_id in projects
        ]
    )


async def share_demo_with_colleagues(session: AsyncSession, project: Project) -> None:
    """Public demo: give the organization's colleagues viewer access to a demo just made ready.

    Acceptance (:func:`_grant_demo_project_access`) shares the demos that are
    ready at that moment; this is the other half, called where a demo becomes
    ready in an organization (a seed promoted, or a pooled demo claimed into
    it), so a demo generated after a colleague joined is shared too.

    The colleagues are the organization's ``member``-role members: on a public
    demo that role is reached only by accepting an invitation, and an owner or
    admin sees every project already. A member who has a row for this project
    keeps it (existing project roles win, as at acceptance). Off a public demo
    this does nothing: there project access stays an explicit decision.

    Takes the organization's owner-set lock, the one acceptance holds, so an
    acceptance racing the promotion either sees the demo ready or is seen here
    as a member; it cannot miss both. Does NOT commit.
    """
    if not tenancy.public_demo() or not project.is_demo:
        return
    await auth_service.acquire_owner_set_xact_lock(session, project.organization_id)
    colleagues = await session.scalars(
        select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == project.organization_id,
            OrganizationMember.role == OrganizationRole.member.value,
            ~select(ProjectMember.id)
            .where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == OrganizationMember.user_id,
            )
            .exists(),
        )
    )
    session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=user_id,
                role="viewer",
                added_by_user_id=project.created_by_user_id,
            )
            for user_id in colleagues
        ]
    )
    await session.flush()
