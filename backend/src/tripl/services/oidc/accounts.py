"""Taking over an account nobody proved the address of, for a provider-verified sign-in.

A hosted sign-up can sit unverified: someone typed an address they may not own.
When a provider later vouches for that address (Google, an organization's
identity provider, SCIM), the person it vouches for takes the account over
clean, and whoever registered it loses everything they held.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.api_key import ApiKey
from tripl.models.email_verification_token import EmailVerificationToken
from tripl.models.password_reset_token import PasswordResetToken
from tripl.models.user import User
from tripl.models.user_session import UserSession
from tripl.services.oidc.flow import unusable_password_hash


def is_unclaimed(user: User) -> bool:
    """An account nobody has proved the address of: a hosted sign-up still unverified.

    Self-hosted accounts are verified at creation, and a platform admin is
    never treated as unclaimed.
    """
    return user.email_verified_at is None and not user.is_platform_admin


async def reclaim(session: AsyncSession, user: User, now: datetime) -> None:
    """Take over an unclaimed account for the provider-verified person. No commit.

    Whoever registered the address without proving it loses everything they
    held: the password becomes unusable, and every session, API key, password
    reset and verification token of the account goes.
    """
    user.password_hash = await unusable_password_hash()
    for model in (UserSession, PasswordResetToken, EmailVerificationToken):
        await session.execute(
            delete(model)
            .where(model.user_id == user.id)
            .execution_options(synchronize_session=False)
        )
    await session.execute(
        update(ApiKey)
        .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )


async def reclaim_if_unclaimed(session: AsyncSession, user: User) -> bool:
    """Take ``user`` over clean if nobody ever proved the address (:func:`is_unclaimed`).

    Returns whether it did. No commit.
    """
    if not is_unclaimed(user):
        return False
    await reclaim(session, user, datetime.now(UTC))
    return True
