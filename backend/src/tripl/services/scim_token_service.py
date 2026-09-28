"""An organization's SCIM bearer tokens and the SCIM request authentication (F20).

A token is ``tripl_scim_`` plus 32 url-safe random bytes. Only its keyed HMAC is
stored (:func:`tripl.auth_utils.hash_session_token`, the session-token idiom):
a leaked table is useless without the operator's ``SECRET_KEY``. The raw token
is returned once, at creation.

:func:`authenticate` is the whole SCIM gate. It admits ONLY such a token —
never a session cookie, never an API key — and only for the organization the
path names:

* no ``Authorization: Bearer``, an API key, an unknown or revoked token: 401;
* a live token of ANOTHER organization, an unknown slug, an organization being
  deleted: 404, so a token is no oracle for which organizations exist;
* the token's own organization suspended: 403;
* a token whose creator is no longer an OWNER of the organization (removed,
  demoted, account deleted): 401. The removal and demotion paths also revoke
  such tokens outright (:func:`revoke_tokens_created_by`); this check is the
  backstop for any path that changes a role without calling it.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.auth_utils import hash_session_token
from tripl.models.domain_enums import OrganizationRole, OrganizationStatus
from tripl.models.org_scim import SCIM_TOKEN_PREFIX, OrgScimToken
from tripl.models.organization import Organization, OrganizationMember
from tripl.models.user import User
from tripl.schemas.org_scim import OrgScimTokenResponse
from tripl.services import audit_service
from tripl.services.org_resolution import ORG_IS_VISIBLE
from tripl.services.scim_errors import ScimError

_TOKEN_BYTES = 32
#: Characters of the secret kept in the display prefix.
_PREFIX_SECRET_CHARS = 6
#: ``last_used_at`` is written at most this often per token (the hot path).
TOUCH_INTERVAL = timedelta(seconds=60)
#: Live tokens an organization may hold at once: rotation needs two, not dozens.
MAX_LIVE_TOKENS = 5

UNAUTHORIZED = "A valid SCIM bearer token is required"
ORG_NOT_FOUND = "Organization not found"
ORG_SUSPENDED = "This organization is suspended"


class TooManyTokensError(Exception):
    """The organization already holds :data:`MAX_LIVE_TOKENS` live tokens."""


class TokenNotFoundError(LookupError):
    """No such token in the organization."""


@dataclass(frozen=True)
class ScimCaller:
    """Who a SCIM request acts as: a token of one organization."""

    organization_id: uuid.UUID
    organization_slug: str
    token_id: uuid.UUID
    token_prefix: str

    def audit_payload(self, **extra: object) -> dict[str, object]:
        """The payload every SCIM write files: ``via`` and the token's prefix, then ``extra``."""
        return {"via": "scim", "token_prefix": self.token_prefix, **extra}


def _digest(raw: str) -> str:
    return hash_session_token(raw)


def _new_token() -> tuple[str, str]:
    raw = SCIM_TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
    return raw, raw[: len(SCIM_TOKEN_PREFIX) + _PREFIX_SECRET_CHARS]


async def list_tokens(session: AsyncSession, org_id: uuid.UUID) -> list[OrgScimTokenResponse]:
    """Every token of the organization, newest first, revoked ones included."""
    rows = (
        await session.execute(
            select(OrgScimToken, User.email)
            .outerjoin(User, User.id == OrgScimToken.created_by)
            .where(OrgScimToken.organization_id == org_id)
            .order_by(OrgScimToken.created_at.desc(), OrgScimToken.id)
        )
    ).all()
    return [token_response(token, email) for token, email in rows]


def token_response(token: OrgScimToken, created_by_email: str | None) -> OrgScimTokenResponse:
    return OrgScimTokenResponse(
        id=token.id,
        prefix=token.prefix,
        created_at=token.created_at,
        created_by_email=created_by_email,
        last_used_at=token.last_used_at,
        revoked_at=token.revoked_at,
    )


async def count_live_tokens(session: AsyncSession, org_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count()).where(
            OrgScimToken.organization_id == org_id, OrgScimToken.revoked_at.is_(None)
        )
    )
    return int(count or 0)


async def create_token(
    session: AsyncSession, org_id: uuid.UUID, *, created_by: uuid.UUID
) -> tuple[OrgScimToken, str]:
    """A new token row (flushed, not committed) and the raw token."""
    if await count_live_tokens(session, org_id) >= MAX_LIVE_TOKENS:
        raise TooManyTokensError
    raw, prefix = _new_token()
    now = datetime.now(UTC)
    token = OrgScimToken(
        organization_id=org_id,
        token_hash=_digest(raw),
        prefix=prefix,
        created_by=created_by,
        last_used_at=None,
        revoked_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(token)
    await session.flush()
    return token, raw


async def revoke_token(
    session: AsyncSession, org_id: uuid.UUID, token_id: uuid.UUID
) -> tuple[OrgScimToken, bool]:
    """Revoke the token; returns it and whether it was live. No commit.

    Another organization's token id answers :class:`TokenNotFoundError` like
    an unknown one.
    """
    token: OrgScimToken | None = await session.scalar(
        select(OrgScimToken).where(
            OrgScimToken.organization_id == org_id, OrgScimToken.id == token_id
        )
    )
    if token is None:
        raise TokenNotFoundError(token_id)
    if token.revoked_at is not None:
        return token, False
    token.revoked_at = datetime.now(UTC)
    await session.flush()
    return token, True


#: Audit payload ``reason`` of a token revoked because its creator stopped being an owner.
REVOKE_REASON_CREATOR_NOT_OWNER = "creator_no_longer_owner"


async def revoke_tokens_created_by(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    """Revoke the live tokens ``user_id`` created in ``org_id``; returns how many.

    Called when the user stops being an owner of the organization (removed,
    deprovisioned, demoted, stepped down by a transfer): a token is an owner's
    credential, and one kept by a former owner would let them provision
    themselves back in. Each revocation files an ``org.scim.token_revoke`` row
    (no user, ``reason`` in the payload) WITHOUT committing: the caller commits
    with the change that caused it.
    """
    tokens = list(
        (
            await session.scalars(
                select(OrgScimToken).where(
                    OrgScimToken.organization_id == org_id,
                    OrgScimToken.created_by == user_id,
                    OrgScimToken.revoked_at.is_(None),
                )
            )
        ).all()
    )
    now = datetime.now(UTC)
    for token in tokens:
        token.revoked_at = now
        await audit_service.record(
            session,
            user=None,
            action="org.scim.token_revoke",
            target_type="scim_token",
            target_id=token.id,
            target_name=token.prefix,
            payload={"token_prefix": token.prefix, "reason": REVOKE_REASON_CREATOR_NOT_OWNER},
            commit=False,
            organization_id=org_id,
        )
    if tokens:
        await session.flush()
    return len(tokens)


async def _creator_is_owner(session: AsyncSession, token: OrgScimToken) -> bool:
    if token.created_by is None:
        return False
    role = await session.scalar(
        select(OrganizationMember.role).where(
            OrganizationMember.organization_id == token.organization_id,
            OrganizationMember.user_id == token.created_by,
        )
    )
    return role is not None and str(role) == OrganizationRole.owner.value


def _bearer(authorization: str) -> str | None:
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def _unauthorized() -> ScimError:
    return ScimError(401, UNAUTHORIZED, headers={"WWW-Authenticate": 'Bearer realm="scim"'})


async def authenticate(session: AsyncSession, *, org_slug: str, authorization: str) -> ScimCaller:
    """The SCIM caller of ``authorization`` in the organization ``org_slug``, or a ScimError."""
    raw = _bearer(authorization)
    # Anything but a SCIM token — an API key, a session token pasted in, a JWT —
    # is refused before a lookup, and so is a NUL no stored digest can match.
    if raw is None or not raw.startswith(SCIM_TOKEN_PREFIX) or "\x00" in raw:
        raise _unauthorized()
    token: OrgScimToken | None = await session.scalar(
        select(OrgScimToken).where(
            OrgScimToken.token_hash == _digest(raw), OrgScimToken.revoked_at.is_(None)
        )
    )
    if token is None or not await _creator_is_owner(session, token):
        raise _unauthorized()
    row = (
        await session.execute(
            select(Organization.id, Organization.slug, Organization.status).where(
                Organization.id == token.organization_id, ORG_IS_VISIBLE
            )
        )
    ).first()
    if row is None or row[1] != org_slug:
        raise ScimError(404, ORG_NOT_FOUND)
    org_id, slug, org_status = row
    if str(org_status) == OrganizationStatus.suspended.value:
        raise ScimError(403, ORG_SUSPENDED)
    caller = ScimCaller(
        organization_id=org_id,
        organization_slug=slug,
        token_id=token.id,
        token_prefix=token.prefix,
    )
    now = datetime.now(UTC)
    if token.last_used_at is None or now - token.last_used_at >= TOUCH_INTERVAL:
        token.last_used_at = now
        await session.commit()
    return caller
