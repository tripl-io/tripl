"""Email verification (F20 hosted sign-up, GH #273).

An account proves it owns its address by opening a single-use, expiring link
(:func:`issue_token` / :func:`confirm`) while signed in as that account, or by
a confirmed password reset (the reset link was mailed to the address). A
self-hosted instance records every new account as verified at creation: it
enforces nothing, and may have no SMTP to verify with. Every path goes through
:func:`mark_verified`.

``users.email_verified_at`` is stored in both deployment modes but ENFORCED only
when ``DEPLOYMENT_MODE=hosted`` (:func:`verification_required`): there a
session whose address is unverified is refused everywhere except
``/api/v1/auth/*`` (``api.deps.get_current_user``). Self-hosted instances keep
working exactly as before.

On a hosted instance, the address owner's own proof is what grants
``is_platform_admin`` to an address listed in ``PLATFORM_ADMIN_EMAILS``
(:func:`grant_listed_platform_admin`): a verification link confirmed from a
session of the same account (:func:`confirm`), or a sign-in through an identity
provider that vouches for the address (``instance_login.sign_in_verified``).
Sign-up, invitation redemption and password reset never do. A reset link or an
invitation link can reach someone other than the address owner (an inviter
receives the raw invitation link), so neither is trusted with that grant.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import tenancy
from tripl.auth_utils import hash_session_token, normalize_email
from tripl.config import settings
from tripl.models.email_verification_token import EmailVerificationToken
from tripl.models.user import User
from tripl.models.user_session import UserSession

# Long enough to survive "I'll click it tomorrow morning"; the link only proves
# the address, it grants no access by itself.
EMAIL_VERIFICATION_TTL_HOURS = 24
# Same generator and width as session, reset and invitation tokens (~256 bits).
EMAIL_VERIFICATION_TOKEN_BYTES = 32

#: The hosted gate's refusal (``api.deps.get_current_user``).
EMAIL_NOT_VERIFIED_MESSAGE = "Email address not verified"
# Identical for unknown / expired / used tokens so confirm never reveals which.
INVALID_VERIFICATION_MESSAGE = "This verification link is invalid or has expired."


def verification_required() -> bool:
    """Whether this instance enforces email verification: multi-tenant only."""
    return tenancy.multi_tenant()


def is_blocked(user: User) -> bool:
    """Whether the hosted gate refuses ``user``: enforced and not yet verified."""
    return verification_required() and user.email_verified_at is None


def build_verification_link(app_base_url: str, raw_token: str) -> str:
    """The SPA's ``/verify-email`` page, which POSTs the token to confirm.

    ``app_base_url`` should be set whenever email is configured; if blank the
    link degrades to a relative path, as the password reset link does.
    """
    return f"{app_base_url.rstrip('/')}/verify-email?token={raw_token}"


def mark_verified(user: User, *, now: datetime | None = None) -> bool:
    """Record that ``user`` owns their address, if not already recorded. No flush.

    Grants nothing else: ``PLATFORM_ADMIN_EMAILS`` is honoured only where the
    address owner proved the address (see :func:`grant_listed_platform_admin`).
    Returns whether the account changed from unverified to verified.
    """
    if user.email_verified_at is not None:
        return False
    user.email_verified_at = now or datetime.now(UTC)
    return True


def grant_listed_platform_admin(user: User) -> bool:
    """On a hosted instance, make ``user`` a platform admin if its address is listed.

    Called only where the address owner proved the address: from
    :func:`confirm`, once a verification link was redeemed from a session of
    this very account, and from ``instance_login.sign_in_verified``, once an
    identity provider vouched for it (``email_verified``). Never from sign-up,
    invitation redemption or password reset. Returns whether the flag changed.
    """
    if not verification_required() or user.is_platform_admin:
        return False
    if normalize_email(user.email) not in settings.platform_admin_emails:
        return False
    user.is_platform_admin = True
    return True


def _hash_token(raw_token: str) -> str:
    return hash_session_token(raw_token)


async def _delete_tokens_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, exclude_id: uuid.UUID | None = None
) -> None:
    statement = delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id)
    if exclude_id is not None:
        statement = statement.where(EmailVerificationToken.id != exclude_id)
    await session.execute(statement.execution_options(synchronize_session=False))


async def issue_token(session: AsyncSession, user: User) -> str:
    """Mint a verification token for ``user`` and return the raw value. Does NOT commit.

    Every earlier token of the user goes first, so only the newest link works
    (a resend supersedes the previous mail, as password resets do).
    """
    await _delete_tokens_for_user(session, user.id)
    raw_token = secrets.token_urlsafe(EMAIL_VERIFICATION_TOKEN_BYTES)
    session.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(hours=EMAIL_VERIFICATION_TTL_HOURS),
        )
    )
    await session.flush()
    return raw_token


async def confirm(
    session: AsyncSession,
    raw_token: str,
    *,
    session_user: User,
    session_token_hash: str,
) -> User:
    """Consume a verification token for the signed-in ``session_user``. Commits.

    Unknown, expired and already-used tokens are the same 400, and so is a
    token of ANOTHER account — which is left unconsumed, so its owner can
    still use it. On success the user is marked verified, a listed
    ``PLATFORM_ADMIN_EMAILS`` address becomes a platform admin (hosted), the
    token is marked used, every other token of the user is dropped, and every
    other session of the user is signed out (the one identified by
    ``session_token_hash`` stays): the address owner has now proved control,
    so whoever else signed in with that address before is dropped.
    """
    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_VERIFICATION_MESSAGE
    )
    row = cast(
        EmailVerificationToken | None,
        await session.scalar(
            select(EmailVerificationToken).where(
                EmailVerificationToken.token_hash == _hash_token(raw_token)
            )
        ),
    )
    now = datetime.now(UTC)
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise invalid
    if row.user_id != session_user.id:
        raise invalid
    user = await session.get(User, row.user_id)
    if user is None:
        raise invalid
    mark_verified(user, now=now)
    grant_listed_platform_admin(user)
    row.used_at = now
    await _delete_tokens_for_user(session, user.id, exclude_id=row.id)
    await session.execute(
        delete(UserSession)
        .where(
            UserSession.user_id == user.id,
            UserSession.session_token_hash != session_token_hash,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return user
