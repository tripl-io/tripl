"""Who is a platform admin: the ``tripl-admin`` console script's grants.

The flag is the operator's: it opens Settings → Instance and, with the
Enterprise platform console, every organization on the instance. The console
grants and revokes it in a browser; the server's own shell does it here
(``tripl.admin_cli``). Both refuse to revoke the last platform admin and write
the same ``platform.admin_grant`` / ``platform.admin_revoke`` rows, which have
no organization (platform scope).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, null, select
from sqlalchemy.orm import Session

from tripl.models.audit_log import AuditLog
from tripl.models.user import User

USER_NOT_FOUND = "User not found"
LAST_PLATFORM_ADMIN = "Cannot revoke the last platform admin"


class PlatformUserNotFoundError(LookupError):
    """No such account."""


class PlatformAdminConflictError(Exception):
    """A grant or revoke refused; the message is the one to show."""


class LastPlatformAdminError(PlatformAdminConflictError):
    """Revoking the only platform admin left."""


@dataclass(frozen=True)
class AdminChange:
    """What a grant or revoke did: the user, and whether the flag moved."""

    user: User
    changed: bool


def admin_ids_for_update() -> Select[tuple[uuid.UUID]]:
    # FOR UPDATE on every admin row serializes concurrent revokes: the second
    # waits, then re-reads the set without the admin the first one revoked, so
    # two admins revoking each other cannot leave the instance without one.
    return select(User.id).where(User.is_platform_admin.is_(True)).with_for_update()


def admin_audit_row(
    target: User, *, grant: bool, actor: User | None, via: str, marked_verified: bool = False
) -> AuditLog:
    """The platform-scope audit row of a grant or revoke (``organization_id`` NULL)."""
    payload: dict[str, object] = {"email": target.email, "via": via}
    if marked_verified:
        payload["marked_verified"] = True
    entry = AuditLog(
        user_id=actor.id if actor else None,
        user_email=actor.email if actor else "",
        project_id=None,
        project_slug="",
        branch_id=None,
        branch_name="",
        action="platform.admin_grant" if grant else "platform.admin_revoke",
        target_type="user",
        target_id=target.id,
        target_name=target.email[:255],
        payload=payload,
    )
    entry.organization_id = null()
    return entry


def set_platform_admin_sync(session: Session, email: str, *, grant: bool) -> AdminChange:
    """Grant or revoke the flag for the ``tripl-admin`` console script. Commits.

    Keyed by email; the operator at the shell is not an account, so the audit
    row has no user. The last platform admin cannot be revoked here either.

    A grant also marks the address verified when it is not yet: the operator
    controls the instance and vouches for the account (hosted sign-in may
    require a verified address). The audit row says so
    (``{"marked_verified": true}``). An account that is already a platform
    admin but unverified is only marked verified: no grant row, since nothing
    was granted.
    """
    target: User | None = session.scalar(select(User).where(User.email == email))
    if target is None:
        raise PlatformUserNotFoundError(email)
    marked_verified = grant and target.email_verified_at is None
    if marked_verified:
        target.email_verified_at = datetime.now(UTC)
    if bool(target.is_platform_admin) == grant:
        if marked_verified:
            session.commit()
            session.refresh(target)
        return AdminChange(user=target, changed=False)
    if not grant:
        admins = set(session.scalars(admin_ids_for_update()).all())
        if target.id not in admins:
            session.rollback()
            session.refresh(target)
            return AdminChange(user=target, changed=False)
        if admins == {target.id}:
            session.rollback()
            raise LastPlatformAdminError(LAST_PLATFORM_ADMIN)
    target.is_platform_admin = grant
    session.add(
        admin_audit_row(
            target, grant=grant, actor=None, via="tripl-admin", marked_verified=marked_verified
        )
    )
    session.commit()
    session.refresh(target)
    return AdminChange(user=target, changed=True)


def list_platform_admins_sync(session: Session) -> list[User]:
    """Every platform admin, by email."""
    return list(
        session.scalars(
            select(User).where(User.is_platform_admin.is_(True)).order_by(User.email)
        ).all()
    )
