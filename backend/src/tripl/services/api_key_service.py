"""User-issued API keys for non-browser clients (LLM agents, CLI scripts).

The raw token shape is ``tk_<scope-letter>_<24 url-safe random chars>`` so the
prefix the UI shows in lists tells the operator at a glance which scope a key
carries without exposing the secret. Only the sha-256 hash of the raw token
is stored — a leaked DB dump can't replay tokens.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.bucketing import optional_to_utc
from tripl.middleware.org_context import require_org_id
from tripl.models.api_key import ApiKey
from tripl.models.domain_enums import ApiKeyScope
from tripl.models.project import Project

ALLOWED_SCOPES = tuple(scope.value for scope in ApiKeyScope)
_PREFIX = "tk_"
_RANDOM_LEN = 24

# Throttle ``last_used_at`` writes on the auth hot path: every authenticated
# request would otherwise issue a commit just to bump this column. We only
# refresh it once the recorded value is older than this interval.
API_KEY_TOUCH_INTERVAL_SECONDS = 60


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_token(scope: str) -> tuple[str, str]:
    """Return ``(raw_token, key_prefix)``.

    Embeds the scope letter in the token so support can eyeball which scope
    a leaked key carried without DB access (e.g. ``tk_r_...`` vs ``tk_w_...``).
    """
    body = secrets.token_urlsafe(_RANDOM_LEN)
    raw = f"{_PREFIX}{scope[0]}_{body}"
    return raw, raw[: len(_PREFIX) + 2 + 6]  # tk_<scope>_<first 6 chars>


async def list_keys(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> list[ApiKey]:
    """``user_id``'s keys bound to ``organization_id`` (the request's org), newest first.

    A key acts in exactly one organization, so listing it under another would
    show a key that does nothing there.
    """
    rows = await session.execute(
        select(ApiKey)
        .where(ApiKey.user_id == user_id, ApiKey.organization_id == organization_id)
        .order_by(ApiKey.created_at.desc())
    )
    return list(rows.scalars().all())


async def create_key(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    name: str,
    scope: str,
    expires_in_days: int | None,
    project_id: uuid.UUID | None = None,
    created_with_sso_org_id: uuid.UUID | None = None,
) -> tuple[ApiKey, str]:
    """Create a key row and return ``(row, raw_token)``.

    The raw token is only ever surfaced once — the row carries the prefix
    plus the hash, so callers should display the raw token to the operator
    immediately and never again. ``created_with_sso_org_id`` is the
    organization whose SSO session mints the key (F20), if any.
    """
    if scope not in ALLOWED_SCOPES:
        raise HTTPException(status_code=422, detail=f"scope must be one of {list(ALLOWED_SCOPES)}")
    normalized_name = name.strip()
    if not normalized_name:
        raise HTTPException(status_code=422, detail="name is required")
    if expires_in_days is not None and expires_in_days <= 0:
        raise HTTPException(status_code=422, detail="expires_in_days must be > 0")

    raw, prefix = _generate_token(scope)
    expires_at = (
        datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days is not None else None
    )
    created_at = datetime.now(UTC)
    row = ApiKey(
        user_id=user_id,
        project_id=project_id,
        # The bound organization; with none bound this raises rather than
        # defaulting (F20 PR5). A project-bound key's project was resolved in
        # this same organization, and fk_api_keys_project_organization holds it.
        organization_id=require_org_id(),
        name=normalized_name,
        key_prefix=prefix,
        key_hash=_hash_token(raw),
        scope=scope,
        expires_at=expires_at,
        created_with_sso_org_id=created_with_sso_org_id,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, raw


async def revoke_key(
    session: AsyncSession, user_id: uuid.UUID, key_id: uuid.UUID
) -> tuple[ApiKey, str | None] | None:
    """Mark a key revoked. Idempotent: revoking an already-revoked row is a no-op."""
    row = await session.get(ApiKey, key_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="API key not found")
    if row.revoked_at is not None:
        return None
    project_slug = (
        await session.scalar(select(Project.slug).where(Project.id == row.project_id))
        if row.project_id is not None
        else None
    )
    row.revoked_at = datetime.now(UTC)
    await session.commit()
    return row, project_slug


async def verify_and_touch(session: AsyncSession, raw_token: str) -> ApiKey | None:
    """Resolve a raw bearer token to its (live, non-expired, non-revoked) row.

    Updates ``last_used_at`` on a successful hit so the UI can show recent
    activity. Returns ``None`` if the token is unknown / revoked / expired so
    the caller picks the rejection reason (we don't want to leak which case
    it is in error responses)."""
    if not raw_token.startswith(_PREFIX):
        return None
    row = await session.scalar(select(ApiKey).where(ApiKey.key_hash == _hash_token(raw_token)))
    if row is None:
        return None
    if row.revoked_at is not None:
        return None
    if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
        return None
    now = datetime.now(UTC)
    # Skip the write (and its commit) on the hot path unless the recorded
    # timestamp is missing or has gone stale, avoiding write amplification
    # from every authenticated request.
    last_used = optional_to_utc(row.last_used_at)
    if last_used is None or (now - last_used) >= timedelta(seconds=API_KEY_TOUCH_INTERVAL_SECONDS):
        row.last_used_at = now
        await session.commit()
    return row
