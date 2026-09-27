from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tripl.auth_utils import (
    hash_password,
    hash_session_token,
    new_session_token,
    normalize_email,
    password_hash_needs_rehash,
    verify_password,
)
from tripl.config import DEPLOYMENT_SELF_HOSTED, REGISTRATION_OPEN, settings
from tripl.middleware.org_context import current_org_id
from tripl.models.domain_enums import OrganizationRole
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.password_reset_token import PasswordResetToken
from tripl.models.user import User
from tripl.models.user_session import UserSession
from tripl.schemas.auth import (
    AuthUserResponse,
    LoginRequest,
    OrgMembershipOut,
    RegisterRequest,
)
from tripl.services import app_settings_service

# Password-reset link lifetime. Short on purpose: a reset link is a bearer
# credential, so it should be usable just long enough for a human to open their
# inbox and click through.
PASSWORD_RESET_TTL_HOURS = 1
# 32 bytes ≈ 256 bits of entropy via ``secrets.token_urlsafe`` — the same
# generator and width as session tokens, so a reset token is never guessable.
PASSWORD_RESET_TOKEN_BYTES = 32

# Shown when a closed instance refuses a signup. Names the exact lever so the
# would-be member can tell an owner what to do, without hinting at whether the
# submitted address already has an account.
REGISTRATION_CLOSED_MESSAGE = (
    "Registration is closed on this instance. Ask an owner to enable it under "
    "Settings -> Instance -> Security & access (or set REGISTRATION_MODE=open) "
    "to create an account."
)

# Single neutral message for both the "we emailed you" and "no such account"
# cases so the request endpoint never reveals whether an address is registered.
PASSWORD_RESET_NEUTRAL_MESSAGE = (
    "If an account exists for that email, a password reset link is on its way."
)
# Deliberately identical for invalid / expired / already-used tokens so confirm
# never leaks which of those a rejected token hit.
_PASSWORD_RESET_INVALID_MESSAGE = "This password reset link is invalid or has expired."

# Tag mixed into every owner-set advisory-lock key. The key is per organization
# (F20 PR4): who owns org A has nothing to do with who owns org B, so their
# owner changes need not wait for each other. Hashed with a fixed tag so the
# keys are stable across releases and unlikely to collide with the per-project
# locks (which derive their keys from raw UUID bytes, see
# demo_runtime._acquire_project_xact_lock).
_OWNER_SET_LOCK_TAG = b"trplown1"
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def _normalize_name(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _session_expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)


async def _get_user_by_email(session: AsyncSession, email: str) -> User | None:
    statement = select(User).where(User.email == email)
    return cast(User | None, await session.scalar(statement))


async def has_any_users(session: AsyncSession) -> bool:
    """Whether the instance has at least one registered user.

    Single source of truth for the "fresh instance" check: powers both the
    first-user-becomes-owner rule and the unauthenticated /auth/status endpoint.
    """
    user_count = await session.scalar(select(func.count()).select_from(User))
    return bool(user_count)


async def _create_user_session(session: AsyncSession, user_id: uuid.UUID) -> str:
    session_token = new_session_token()
    session.add(
        UserSession(
            user_id=user_id,
            session_token_hash=hash_session_token(session_token),
            expires_at=_session_expires_at(),
        )
    )
    await session.flush()
    return session_token


async def create_session_for_user(session: AsyncSession, user_id: uuid.UUID) -> str:
    """Issue a session token for an existing user, for flows outside this module.

    Exists so ``invitation_service`` can log the invitee straight in after
    redeeming, without reaching across a module boundary into a private helper
    or growing a second copy of the TTL and hashing rules. Does not commit —
    the caller owns the transaction.
    """
    return await _create_user_session(session, user_id)


def owner_set_lock_key(org_id: uuid.UUID) -> int:
    """The signed 64-bit advisory-lock key guarding ``org_id``'s owner set."""
    digest = hashlib.blake2b(_OWNER_SET_LOCK_TAG + org_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def acquire_owner_set_xact_lock(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Serialise every change to who owns ``org_id``: entry to the set, and exit.

    Registration takes the default organization's lock BEFORE the
    ``has_any_users`` check so two concurrent first registrations can't both
    observe an empty users table and both become owner (TOCTOU). The same key
    guards the demotion side in ``user_service.update_org_role``, where two
    concurrent demotions of an organization's last two owners would otherwise
    each see the other as the survivor and leave it with none. One invariant per
    organization, one lock per organization; a caller only ever holds one, so
    the locks cannot deadlock against each other.

    PostgreSQL-only, same idiom as
    ``demo_runtime._acquire_project_xact_lock``: SQLite (tests) has no advisory
    locks, so this is a no-op there — the suite runs on a single in-memory
    connection where the race cannot occur, so tests are unaffected. The lock
    auto-releases when the surrounding transaction commits or rolls back.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": owner_set_lock_key(org_id)},
    )


async def is_registration_allowed(session: AsyncSession, *, is_first_user: bool) -> bool:
    """Whether POST /auth/register would be accepted right now.

    The first-owner bootstrap on an empty instance is always allowed — otherwise
    an instance whose owner had closed registration could never be re-claimed
    after a reset. Every later signup needs the instance to be in "open" mode
    (the shipped default; see ``Settings.registration_mode``).
    """
    if is_first_user:
        return True
    return await app_settings_service.get_registration_mode(session) == REGISTRATION_OPEN


def add_organization_membership(
    session: AsyncSession,
    user: User,
    *,
    organization_id: uuid.UUID,
    org_role: OrganizationRole,
) -> OrganizationMember:
    """Stage ``user``'s membership row; the caller flushes and commits.

    Every user creation writes one: the membership is the user's only source of
    rights (F20 PR4), and without it they would get 404 on every
    ``/api/v1/orgs/{org}/...`` URL (and 400 on a hosted instance).
    """
    member = OrganizationMember(
        organization_id=organization_id,
        user_id=user.id,
        role=OrganizationRole(org_role).value,
    )
    session.add(member)
    return member


async def register_user(session: AsyncSession, data: RegisterRequest) -> tuple[User, str]:
    """Self-service sign-up into the default organization.

    On a self-hosted instance the first user becomes the default organization's
    owner AND a platform admin, so the instance always has someone who can
    manage members and the operator settings. Every later user (and every user
    of a hosted instance) joins as ``member``. Self-service sign-up never grants
    platform admin on a hosted instance: nothing here proves the caller owns the
    address, so ``PLATFORM_ADMIN_EMAILS`` is applied only operator-side (by the
    organization migrations, to accounts that already exist).

    The advisory lock closes the TOCTOU window: taken before the empty-table
    check and held until this registration's commit, so a concurrent first
    registration waits and then observes this user — exactly one owner.
    ``users.role`` is not written: nothing reads it any more.
    """
    email = normalize_email(data.email)

    await acquire_owner_set_xact_lock(session, DEFAULT_ORG_ID)
    is_first_user = not await has_any_users(session)

    # Checked BEFORE the duplicate-email lookup on purpose: on a closed instance
    # every anonymous signup attempt gets the same 403, so the endpoint can't be
    # used to probe which addresses are registered.
    if not await is_registration_allowed(session, is_first_user=is_first_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=REGISTRATION_CLOSED_MESSAGE,
        )

    existing = await _get_user_by_email(session, email)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email already exists",
        )

    self_hosted = settings.deployment_mode == DEPLOYMENT_SELF_HOSTED
    bootstrap = is_first_user and self_hosted
    user = User(
        email=email,
        name=_normalize_name(data.name),
        password_hash=await asyncio.to_thread(hash_password, data.password),
        is_platform_admin=bootstrap,
    )
    session.add(user)
    await session.flush()
    add_organization_membership(
        session,
        user,
        organization_id=DEFAULT_ORG_ID,
        org_role=OrganizationRole.owner if bootstrap else OrganizationRole.member,
    )

    session_token = await _create_user_session(session, user.id)
    await session.commit()
    await session.refresh(user)
    return user, session_token


async def _membership_rows(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, str, str]]:
    rows = await session.execute(
        select(Organization.id, Organization.slug, Organization.name, OrganizationMember.role)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == user_id)
        .order_by(Organization.name, Organization.slug)
    )
    return [(org_id, slug, name, str(role)) for org_id, slug, name, role in rows.all()]


async def user_org_memberships(session: AsyncSession, user_id: uuid.UUID) -> list[OrgMembershipOut]:
    """Every organization ``user_id`` belongs to, with their role there."""
    return [
        OrgMembershipOut(slug=slug, name=name, role=OrganizationRole(role))
        for _org_id, slug, name, role in await _membership_rows(session, user_id)
    ]


async def build_auth_user_response(session: AsyncSession, user: User) -> AuthUserResponse:
    """``/auth/me`` (and the login/register answers) for ``user``.

    ``role`` is the user's role in the organization the request acts in: the
    bound one, else the default organization, else the user's only one; ``None``
    when none of those applies. ``orgs`` lists every membership so the UI can
    tell the roles apart once a user belongs to several.
    """
    rows = await _membership_rows(session, user.id)
    by_id = {org_id: role for org_id, _slug, _name, role in rows}
    target = current_org_id()
    role: str | None = None
    if target is not None:
        role = by_id.get(target)
    elif DEFAULT_ORG_ID in by_id:
        role = by_id[DEFAULT_ORG_ID]
    elif len(rows) == 1:
        role = rows[0][3]
    return AuthUserResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        role=None if role is None else OrganizationRole(role),
        is_platform_admin=bool(user.is_platform_admin),
        orgs=[
            OrgMembershipOut(slug=slug, name=name, role=OrganizationRole(org_role))
            for _org_id, slug, name, org_role in rows
        ],
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def authenticate_user(session: AsyncSession, data: LoginRequest) -> tuple[User, str]:
    email = normalize_email(data.email)
    user = await _get_user_by_email(session, email)
    valid_password = await asyncio.to_thread(
        verify_password, data.password, user.password_hash if user else _DUMMY_PASSWORD_HASH
    )
    if user is None or not valid_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Opportunistic rehash: if the stored hash predates a scrypt-cost bump, we
    # have the plaintext in hand, so upgrade it to the current parameters now.
    if password_hash_needs_rehash(user.password_hash):
        user.password_hash = await asyncio.to_thread(hash_password, data.password)

    await session.execute(
        delete(UserSession)
        .where(
            UserSession.user_id == user.id,
            UserSession.expires_at <= datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )
    session_token = await _create_user_session(session, user.id)
    await session.commit()
    await session.refresh(user)
    return user, session_token


async def get_user_by_session_token(session: AsyncSession, session_token: str) -> User | None:
    statement = (
        select(UserSession)
        .options(selectinload(UserSession.user))
        .where(UserSession.session_token_hash == hash_session_token(session_token))
    )
    db_session = cast(UserSession | None, await session.scalar(statement))
    if db_session is None:
        return None

    if db_session.expires_at <= datetime.now(UTC):
        await session.delete(db_session)
        await session.commit()
        return None

    return db_session.user


async def logout_session(session: AsyncSession, session_token: str | None) -> None:
    if session_token is None:
        return

    await session.execute(
        delete(UserSession).where(
            UserSession.session_token_hash == hash_session_token(session_token)
        )
    )
    await session.commit()


def _hash_reset_token(raw_token: str) -> str:
    """Digest a raw reset token for storage/lookup.

    Reuses the shared keyed-HMAC token hasher (``auth_utils.hash_session_token``)
    rather than rolling a new one: only the digest is ever persisted, so a leaked
    ``password_reset_tokens`` column is useless without ``SECRET_KEY``.
    """
    return hash_session_token(raw_token)


def _reset_expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(hours=PASSWORD_RESET_TTL_HOURS)


async def _delete_reset_tokens_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, exclude_id: uuid.UUID | None = None
) -> None:
    """Drop a user's outstanding reset tokens (optionally keeping ``exclude_id``).

    Keeps at most one reset link live per user: called when issuing a new token
    (supersede any earlier link) and on a successful confirm (invalidate every
    sibling of the just-consumed token).
    """
    statement = delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
    if exclude_id is not None:
        statement = statement.where(PasswordResetToken.id != exclude_id)
    await session.execute(statement.execution_options(synchronize_session=False))


async def request_password_reset(session: AsyncSession, email: str) -> tuple[User, str] | None:
    """Issue a single-use reset token for ``email`` when a user exists.

    Returns ``(user, raw_token)`` when an account matches — the caller emails the
    raw token — or ``None`` when no account matches. The caller responds
    identically either way, so this never reveals whether an address is
    registered. Any earlier outstanding token for the user is invalidated so only
    the newest link works.
    """
    user = await _get_user_by_email(session, normalize_email(email))
    if user is None:
        return None

    await _delete_reset_tokens_for_user(session, user.id)
    raw_token = secrets.token_urlsafe(PASSWORD_RESET_TOKEN_BYTES)
    session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_reset_token(raw_token),
            expires_at=_reset_expires_at(),
        )
    )
    await session.commit()
    return user, raw_token


async def confirm_password_reset(session: AsyncSession, raw_token: str, new_password: str) -> None:
    """Consume a reset token and set the user's new password.

    The token must exist, be unexpired and unused. On success the password is
    re-hashed with the shared policy-enforced hasher, the token is marked used
    (single-use), every other outstanding token for the user is dropped, and all
    active sessions are cleared so a reset always ends other logins. Password
    strength is enforced upstream at the schema boundary (same policy as
    register), so an invalid password never reaches here.
    """
    row = cast(
        PasswordResetToken | None,
        await session.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == _hash_reset_token(raw_token)
            )
        ),
    )
    now = datetime.now(UTC)
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_PASSWORD_RESET_INVALID_MESSAGE,
        )

    user = await session.get(User, row.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_PASSWORD_RESET_INVALID_MESSAGE,
        )

    user.password_hash = await asyncio.to_thread(hash_password, new_password)
    row.used_at = now
    await _delete_reset_tokens_for_user(session, user.id, exclude_id=row.id)
    # A password reset invalidates existing sessions: whoever reset the password
    # gets a fresh login, and any other live session is forced to re-authenticate.
    await session.execute(
        delete(UserSession)
        .where(UserSession.user_id == user.id)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
