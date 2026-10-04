"""SCIM ``/Users`` of one organization (F20, GH #273).

A SCIM user is a tripl account; its ``id`` is ``users.id`` and its ``userName``
is the account's (lowercased) email. An organization's SCIM sees exactly the
accounts that are members of it, plus those its provisioning linked and later
deprovisioned (``scim_user_links``, ``active`` false) — never another
organization's users.

Provisioning (``POST``):

* an address with no account gets one only when its domain is one of the
  organization's VERIFIED SSO domains (else 400 ``invalidValue``): a random
  unusable scrypt password (the person signs in through SSO), verified,
  never a platform admin;
* an existing account is linked only when its domain is a verified domain of
  the organization, or it is already a member of it. Any other existing
  account answers the very 400 an unknown address in an unverified domain
  does: an owner's token is no platform-wide oracle for which addresses have
  accounts, and no way to pull a stranger into an organization. A linked
  account joins as ``member``; its password is not set. An account nobody
  ever proved the address of (an unverified hosted sign-up) in a verified
  domain is taken over clean first, exactly as a provider-verified SSO sign-in
  would (:func:`tripl.services.oidc_accounts.reclaim_if_unclaimed`: new
  unusable password, sessions and API keys gone) and marked verified;
* an account with a link row that is not a member (deprovisioned, or DELETEd)
  is re-used by a ``POST``: the link is updated and the user re-activated, so
  a client that deletes and re-creates a user gets the same account back. A
  member already linked answers 409 ``uniqueness``;
* joining lifts the account's SSO membership block — but never for an account
  an owner or admin removed outside SCIM (``removed_outside_scim``, or an
  unlinked account with a membership block): 409 ``mutability`` until an
  accepted invitation brings them back.

``active`` false, and ``DELETE``, deprovision: the membership goes with
everything ``org_service.remove_member`` takes (project grants, the org's API
keys, SSO identities, group memberships, pending invitations); the account and
the link row stay (``active`` false), and ``GET`` still answers the user,
inactive. The last owner cannot be deprovisioned (409 ``mutability``).
``active`` true re-adds the membership as ``member``. A ``PUT`` without
``active`` leaves the state as it is; a ``POST`` without it means active.

``userName`` cannot change (400 ``mutability``): the address is the account's,
across organizations. ``emails`` mirror it; values sent are not applied. Name
parts are echoed from the link row; the account's own ``users.name`` changes
only for an address in the organization's verified domains.

Nothing here commits; the route files the audit row and commits once.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, delete, false, func, inspect, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.auth_utils import hash_password, normalize_email
from tripl.models.domain_enums import OrganizationRole
from tripl.models.org_scim import (
    SCIM_EXTERNAL_ID_MAX_LENGTH,
    SCIM_NAME_PART_MAX_LENGTH,
    ScimUserLink,
)
from tripl.models.organization import OrganizationMember
from tripl.models.organization_group import OrganizationGroup, OrganizationGroupMember
from tripl.models.user import User
from tripl.services import (
    auth_service,
    email_verification_service,
    org_service,
    org_sso_service,
    user_service,
)
from tripl.services.oidc import accounts as oidc_accounts
from tripl.services.scim_errors import INVALID_PATH, MUTABILITY, bad_request, conflict, not_found
from tripl.services.scim_resources import (
    USER_FILTER_ATTRIBUTES,
    USER_SCHEMA,
    PatchOperation,
    excluded_attributes,
    list_response,
    normalize_path,
    optional_string,
    paging,
    parse_bool,
    parse_filter,
    patch_operations,
    timestamp,
)
from tripl.services.scim_token_service import ScimCaller

USER_NOT_FOUND = "User not found"
DOMAIN_NOT_VERIFIED = "email domain not verified for this organization"
USERNAME_IMMUTABLE = "userName cannot be changed; provision the new address as a new user"
LAST_OWNER = "The organization's last owner cannot be deprovisioned"
REMOVED_OUTSIDE_SCIM = (
    "An owner or admin removed this user from the organization; "
    "only an accepted invitation brings them back"
)
ALREADY_PROVISIONED = "This user is already provisioned in this organization"

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EMAIL_MAX_LENGTH = 320
_NAME_MAX_LENGTH = 255
_PASSWORD_BYTES = 32


class _Unset:
    """Marks an attribute the request did not mention."""


UNSET = _Unset()


@dataclass
class UserChanges:
    """What a POST/PUT/PATCH asks for; :data:`UNSET` for what it leaves alone."""

    active: bool | None = None
    external_id: str | None | _Unset = UNSET
    given_name: str | None | _Unset = UNSET
    family_name: str | None | _Unset = UNSET
    formatted: str | None | _Unset = UNSET
    display_name: str | None | _Unset = UNSET
    user_names: list[object] = field(default_factory=list)


@dataclass(frozen=True)
class WriteResult:
    """A write's resource and what the route audits."""

    resource: dict[str, Any]
    user: User
    action: str
    payload: dict[str, object]
    created: bool = False


# ── reading ─────────────────────────────────────────────────────────────────


def _visible(org_id: uuid.UUID) -> ColumnElement[bool]:
    members = select(OrganizationMember.user_id).where(OrganizationMember.organization_id == org_id)
    linked = select(ScimUserLink.user_id).where(ScimUserLink.organization_id == org_id)
    return or_(User.id.in_(members), User.id.in_(linked))


def _parse_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


async def get_visible_user(session: AsyncSession, org_id: uuid.UUID, raw_id: str) -> User:
    user_id = _parse_id(raw_id)
    if user_id is None:
        raise not_found(USER_NOT_FOUND)
    user: User | None = await session.scalar(
        select(User).where(User.id == user_id, _visible(org_id))
    )
    if user is None:
        raise not_found(USER_NOT_FOUND)
    return user


async def _links(
    session: AsyncSession, org_id: uuid.UUID, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, ScimUserLink]:
    if not user_ids:
        return {}
    rows = await session.scalars(
        select(ScimUserLink).where(
            ScimUserLink.organization_id == org_id, ScimUserLink.user_id.in_(list(user_ids))
        )
    )
    return {row.user_id: row for row in rows.all()}


async def _member_ids(
    session: AsyncSession, org_id: uuid.UUID, user_ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    if not user_ids:
        return set()
    rows = await session.scalars(
        select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id.in_(list(user_ids)),
        )
    )
    return set(rows.all())


async def _groups(
    session: AsyncSession, org_id: uuid.UUID, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[uuid.UUID, str]]]:
    if not user_ids:
        return {}
    rows = (
        await session.execute(
            select(OrganizationGroupMember.user_id, OrganizationGroup.id, OrganizationGroup.name)
            .join(OrganizationGroup, OrganizationGroup.id == OrganizationGroupMember.group_id)
            .where(
                OrganizationGroup.organization_id == org_id,
                OrganizationGroupMember.user_id.in_(list(user_ids)),
            )
            .order_by(func.lower(OrganizationGroup.name))
        )
    ).all()
    out: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = {}
    for user_id, group_id, name in rows:
        out.setdefault(user_id, []).append((group_id, name))
    return out


def _resource(
    user: User,
    link: ScimUserLink | None,
    *,
    active: bool,
    groups: list[tuple[uuid.UUID, str]],
    base_url: str,
    excluded: set[str],
) -> dict[str, Any]:
    name: dict[str, str] = {}
    if user.name:
        name["formatted"] = user.name
    if link is not None and link.given_name:
        name["givenName"] = link.given_name
    if link is not None and link.family_name:
        name["familyName"] = link.family_name
    resource: dict[str, Any] = {
        "schemas": [USER_SCHEMA],
        "id": str(user.id),
        "userName": user.email,
        "displayName": user.name or user.email,
        "emails": [{"value": user.email, "type": "work", "primary": True}],
        "active": active,
        "meta": {
            "resourceType": "User",
            "created": timestamp(user.created_at),
            "lastModified": timestamp(
                max(user.updated_at, link.updated_at) if link is not None else user.updated_at
            ),
            "location": f"{base_url}/Users/{user.id}",
        },
    }
    if name:
        resource["name"] = name
    if link is not None and link.external_id:
        resource["externalId"] = link.external_id
    if "groups" not in excluded:
        resource["groups"] = [
            {"value": str(group_id), "display": group_name, "$ref": f"{base_url}/Groups/{group_id}"}
            for group_id, group_name in groups
        ]
    return resource


async def loaded(session: AsyncSession, obj: object) -> None:
    """Load what a flush expired (server-side timestamps) before it is read.

    A row just inserted or updated has ``created_at``/``updated_at`` expired;
    reading one would lazy-load it synchronously, which an async session refuses.
    """
    state = inspect(obj)
    if state is None:
        return
    if state.expired_attributes or {"created_at", "updated_at"} & state.unloaded:
        await session.refresh(obj)


async def render(
    session: AsyncSession,
    org_id: uuid.UUID,
    users: Sequence[User],
    base_url: str,
    excluded: set[str] | None = None,
) -> list[dict[str, Any]]:
    ids = [user.id for user in users]
    links = await _links(session, org_id, ids)
    for user in users:
        await loaded(session, user)
    for link in links.values():
        await loaded(session, link)
    members = await _member_ids(session, org_id, ids)
    groups = await _groups(session, org_id, ids)
    return [
        _resource(
            user,
            links.get(user.id),
            active=user.id in members,
            groups=groups.get(user.id, []),
            base_url=base_url,
            excluded=excluded or set(),
        )
        for user in users
    ]


async def list_users(
    session: AsyncSession, org_id: uuid.UUID, params: Mapping[str, str], base_url: str
) -> dict[str, Any]:
    flt = parse_filter(params.get("filter"), USER_FILTER_ATTRIBUTES)
    start, count = paging(params)
    where: list[ColumnElement[bool]] = [_visible(org_id)]
    if flt is not None:
        if flt.attribute in {"userName", "email"}:
            where.append(User.email == normalize_email(flt.value))
        elif flt.attribute == "externalId":
            where.append(
                User.id.in_(
                    select(ScimUserLink.user_id).where(
                        ScimUserLink.organization_id == org_id,
                        ScimUserLink.external_id == flt.value,
                    )
                )
            )
        else:
            user_id = _parse_id(flt.value)
            where.append(User.id == user_id if user_id is not None else false())
    total = int(await session.scalar(select(func.count()).select_from(User).where(*where)) or 0)
    users: list[User] = []
    if count > 0:
        users = list(
            (
                await session.scalars(
                    select(User)
                    .where(*where)
                    .order_by(User.created_at, User.id)
                    .offset(start - 1)
                    .limit(count)
                )
            ).all()
        )
    resources = await render(session, org_id, users, base_url, excluded_attributes(params))
    return list_response(resources, total=total, start_index=start)


async def get_user(
    session: AsyncSession, org_id: uuid.UUID, raw_id: str, base_url: str
) -> dict[str, Any]:
    user = await get_visible_user(session, org_id, raw_id)
    [resource] = await render(session, org_id, [user], base_url)
    return resource


# ── request bodies ──────────────────────────────────────────────────────────


def _name_part(value: object, attribute: str) -> str | None:
    return optional_string(value, attribute, max_length=SCIM_NAME_PART_MAX_LENGTH)


def _apply_attribute(changes: UserChanges, path: str, value: object, *, op: str) -> None:
    """One attribute of a resource body or a PATCH operation. Unknown attributes are ignored.

    Identity providers send what their attribute mapping holds (titles, phone
    numbers, enterprise extension fields); refusing those would break a
    default Okta or Azure AD setup for attributes tripl has nowhere to keep.
    """
    removing = op == "remove"
    match path:
        case "active":
            if removing:
                raise bad_request("'active' cannot be removed", INVALID_PATH)
            changes.active = parse_bool(value, "active")
        case "externalid":
            changes.external_id = (
                None
                if removing
                else optional_string(value, "externalId", max_length=SCIM_EXTERNAL_ID_MAX_LENGTH)
            )
        case "displayname":
            changes.display_name = (
                None
                if removing
                else optional_string(value, "displayName", max_length=_NAME_MAX_LENGTH)
            )
        case "name.givenname":
            changes.given_name = None if removing else _name_part(value, "name.givenName")
        case "name.familyname":
            changes.family_name = None if removing else _name_part(value, "name.familyName")
        case "name.formatted":
            changes.formatted = None if removing else _name_part(value, "name.formatted")
        case "name":
            if removing or value is None:
                changes.given_name = changes.family_name = changes.formatted = None
                return
            if not isinstance(value, dict):
                raise bad_request("'name' must be an object")
            for key, item in value.items():
                sub = str(key).lower()
                if sub in {"givenname", "familyname", "formatted"}:
                    _apply_attribute(changes, f"name.{sub}", item, op=op)
        case "username":
            if removing:
                raise bad_request(USERNAME_IMMUTABLE, MUTABILITY)
            changes.user_names.append(value)
        case _:
            return


def changes_from_resource(
    body: Mapping[str, Any], *, replace: bool, creating: bool = False
) -> UserChanges:
    """A full User resource (POST, PUT). ``replace``: what it omits is cleared.

    An absent ``active`` is "active" on a ``POST`` (``creating``; RFC 7643
    §4.1.1: absent is not "inactive") and "leave it" on a ``PUT``: a provider
    that simply does not send it must not re-activate a deprovisioned user.
    """
    changes = UserChanges()
    if replace:
        changes.external_id = None
        changes.given_name = changes.family_name = None
    for key, value in body.items():
        _apply_attribute(changes, str(key).lower(), value, op="replace")
    if changes.active is None and creating:
        changes.active = True
    return changes


def changes_from_patch(operations: Sequence[PatchOperation]) -> UserChanges:
    changes = UserChanges()
    for operation in operations:
        if operation.path is None:
            if not isinstance(operation.value, dict):
                raise bad_request("A PATCH operation without a path needs an object value")
            for key, value in operation.value.items():
                _apply_attribute(changes, normalize_path(str(key)), value, op=operation.op)
        else:
            _apply_attribute(changes, operation.path, operation.value, op=operation.op)
    return changes


def _email(value: object) -> str:
    if not isinstance(value, str):
        raise bad_request("'userName' is required and must be an email address")
    email = normalize_email(value)
    if len(email) > _EMAIL_MAX_LENGTH or not _EMAIL.match(email):
        raise bad_request("'userName' must be an email address")
    return email


def _check_user_names(changes: UserChanges, user: User) -> None:
    for value in changes.user_names:
        if not isinstance(value, str) or normalize_email(value) != user.email:
            raise bad_request(USERNAME_IMMUTABLE, MUTABILITY)


def _account_name(changes: UserChanges, link: ScimUserLink | None) -> str | None | _Unset:
    """The ``users.name`` a change implies, or :data:`UNSET` if it implies none."""
    for candidate in (changes.formatted, changes.display_name):
        if not isinstance(candidate, _Unset) and candidate is not None:
            return candidate[:_NAME_MAX_LENGTH]
    if isinstance(changes.given_name, _Unset) and isinstance(changes.family_name, _Unset):
        return UNSET
    given = (
        changes.given_name
        if not isinstance(changes.given_name, _Unset)
        else (link.given_name if link is not None else None)
    )
    family = (
        changes.family_name
        if not isinstance(changes.family_name, _Unset)
        else (link.family_name if link is not None else None)
    )
    joined = " ".join(part for part in (given, family) if part)
    return joined[:_NAME_MAX_LENGTH] or UNSET


# ── writing ─────────────────────────────────────────────────────────────────


async def _domain_verified(session: AsyncSession, org_id: uuid.UUID, email: str) -> bool:
    domain = email.rsplit("@", 1)[-1]
    return domain in await org_sso_service.verified_domains(session, org_id)


async def _is_member(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return bool(await _member_ids(session, org_id, [user_id]))


async def _unusable_password_hash() -> str:
    return await asyncio.to_thread(hash_password, secrets.token_urlsafe(_PASSWORD_BYTES))


async def _join(session: AsyncSession, org_id: uuid.UUID, user: User) -> None:
    """Make ``user`` a plain member again and lift their SSO membership block."""
    auth_service.add_organization_membership(
        session, user, organization_id=org_id, org_role=OrganizationRole.member
    )
    await org_sso_service.lift_membership_block(session, org_id, user.id)
    await session.flush()


async def _set_active(
    session: AsyncSession,
    caller: ScimCaller,
    user: User,
    link: ScimUserLink,
    active: bool,
) -> dict[str, object] | None:
    """Join or deprovision as ``active`` asks; what happened, or ``None`` for nothing.

    A user an owner or admin removed outside SCIM is not re-joined (409
    ``mutability``) until they are a member again, which clears the mark.
    """
    member = await _is_member(session, caller.organization_id, user.id)
    if member:
        link.removed_outside_scim = False
    elif active and link.removed_outside_scim:
        raise conflict(REMOVED_OUTSIDE_SCIM, MUTABILITY)
    link.active = active
    if active and not member:
        await _join(session, caller.organization_id, user)
        return {"transition": "reactivate"}
    if not active and member:
        try:
            removed = await org_service.deprovision_member(session, caller.organization_id, user.id)
        except user_service.LastOwnerError:
            raise conflict(LAST_OWNER, MUTABILITY) from None
        return {
            "transition": "deactivate",
            "old_role": removed.old_role,
            "project_memberships": removed.project_memberships,
            "api_keys": removed.api_keys,
            "invitations": removed.invitations,
            "group_memberships": removed.group_memberships,
            **removed.extension_counts,
        }
    await session.flush()
    return None


async def _apply(
    session: AsyncSession,
    caller: ScimCaller,
    user: User,
    link: ScimUserLink,
    changes: UserChanges,
) -> dict[str, object]:
    """Apply ``changes`` to an existing user; returns what changed, for the audit row."""
    _check_user_names(changes, user)
    changed: dict[str, object] = {}
    if not isinstance(changes.external_id, _Unset) and changes.external_id != link.external_id:
        link.external_id = changes.external_id
        changed["external_id"] = changes.external_id
    if not isinstance(changes.given_name, _Unset) and changes.given_name != link.given_name:
        link.given_name = changes.given_name
        changed["given_name"] = changes.given_name
    if not isinstance(changes.family_name, _Unset) and changes.family_name != link.family_name:
        link.family_name = changes.family_name
        changed["family_name"] = changes.family_name
    name = _account_name(changes, link)
    if (
        not isinstance(name, _Unset)
        and name != user.name
        and await _domain_verified(session, caller.organization_id, user.email)
    ):
        changed["name"] = {"old": user.name, "new": name}
        user.name = name
    if changes.active is not None:
        transition = await _set_active(session, caller, user, link, changes.active)
        if transition is not None:
            changed.update(transition)
    await session.flush()
    return changed


async def _link_for(session: AsyncSession, org_id: uuid.UUID, user: User) -> ScimUserLink:
    """The user's link row, created for a member the provider never provisioned."""
    link = (await _links(session, org_id, [user.id])).get(user.id)
    if link is None:
        link = ScimUserLink(
            organization_id=org_id,
            user_id=user.id,
            external_id=None,
            given_name=None,
            family_name=None,
            active=True,
            removed_outside_scim=False,
        )
        session.add(link)
        await session.flush()
    return link


def _action(changed: Mapping[str, object], *, deleted: bool = False) -> str:
    transition = changed.get("transition")
    if transition == "deactivate" or deleted:
        return "deactivate"
    if transition == "reactivate":
        return "reactivate"
    return "update"


async def create_user(
    session: AsyncSession, caller: ScimCaller, body: Mapping[str, Any], base_url: str
) -> WriteResult:
    org_id = caller.organization_id
    email = _email(body.get("userName"))
    changes = changes_from_resource(body, replace=True, creating=True)
    domain_verified = await _domain_verified(session, org_id, email)
    user: User | None = await session.scalar(select(User).where(User.email == email))
    created = False
    reclaimed = False
    if user is not None:
        existing = (await _links(session, org_id, [user.id])).get(user.id)
        member = await _is_member(session, org_id, user.id)
        if existing is not None:
            if member:
                raise conflict(ALREADY_PROVISIONED)
            return await _relink(session, caller, user, existing, changes, base_url)
        if not domain_verified and not member:
            # The same answer as an unknown address: no existence oracle.
            raise bad_request(DOMAIN_NOT_VERIFIED)
        if not member and await org_sso_service.membership_blocked(session, org_id, user.id):
            raise conflict(REMOVED_OUTSIDE_SCIM, MUTABILITY)
    if user is None:
        if not domain_verified:
            raise bad_request(DOMAIN_NOT_VERIFIED)
        name = _account_name(changes, None)
        user = User(
            email=email,
            name=None if isinstance(name, _Unset) else name,
            password_hash=await _unusable_password_hash(),
            is_platform_admin=False,
        )
        email_verification_service.mark_verified(user)
        try:
            async with session.begin_nested():
                session.add(user)
                await session.flush()
        except IntegrityError:
            raise conflict("A user with this userName already exists") from None
        created = True
    else:
        if domain_verified and user.email_verified_at is None:
            reclaimed = await oidc_accounts.reclaim_if_unclaimed(session, user)
            if reclaimed:
                email_verification_service.mark_verified(user)
    link = ScimUserLink(
        organization_id=org_id,
        user_id=user.id,
        external_id=None if isinstance(changes.external_id, _Unset) else changes.external_id,
        given_name=None if isinstance(changes.given_name, _Unset) else changes.given_name,
        family_name=None if isinstance(changes.family_name, _Unset) else changes.family_name,
        active=bool(changes.active),
        removed_outside_scim=False,
    )
    session.add(link)
    await session.flush()
    transition = await _set_active(session, caller, user, link, bool(changes.active))
    [resource] = await render(session, org_id, [user], base_url)
    payload = caller.audit_payload(
        email=email,
        external_id=link.external_id,
        active=link.active,
        joined=bool(transition and transition.get("transition") == "reactivate"),
    )
    if not created:
        payload["email_verified_by_domain"] = reclaimed
        payload["reclaimed_unverified_account"] = reclaimed
    return WriteResult(
        resource=resource,
        user=user,
        action="provision" if created else "link",
        payload=payload,
        created=True,
    )


async def _relink(
    session: AsyncSession,
    caller: ScimCaller,
    user: User,
    link: ScimUserLink,
    changes: UserChanges,
    base_url: str,
) -> WriteResult:
    """A ``POST`` for a user this organization linked before and who is not a member.

    RFC 7644 §3.6 lets a client re-create what it deleted; the account and its
    link row are the same, so the ``POST`` updates the link and re-activates.
    """
    changed = await _apply(session, caller, user, link, changes)
    [resource] = await render(session, caller.organization_id, [user], base_url)
    return WriteResult(
        resource=resource,
        user=user,
        action=_action(changed),
        payload=caller.audit_payload(email=user.email, relinked=True, changes=changed),
        created=True,
    )


async def replace_user(
    session: AsyncSession,
    caller: ScimCaller,
    raw_id: str,
    body: Mapping[str, Any],
    base_url: str,
) -> WriteResult:
    user = await get_visible_user(session, caller.organization_id, raw_id)
    changes = changes_from_resource(body, replace=True)
    return await _write(session, caller, user, changes, base_url)


async def patch_user(
    session: AsyncSession,
    caller: ScimCaller,
    raw_id: str,
    body: Mapping[str, Any],
    base_url: str,
) -> WriteResult:
    user = await get_visible_user(session, caller.organization_id, raw_id)
    changes = changes_from_patch(patch_operations(body))
    return await _write(session, caller, user, changes, base_url)


async def delete_user(session: AsyncSession, caller: ScimCaller, raw_id: str) -> WriteResult:
    user = await get_visible_user(session, caller.organization_id, raw_id)
    link = await _link_for(session, caller.organization_id, user)
    transition = await _set_active(session, caller, user, link, False)
    return WriteResult(
        resource={},
        user=user,
        action="deactivate",
        payload=caller.audit_payload(email=user.email, deleted=True, **(transition or {})),
    )


async def _write(
    session: AsyncSession,
    caller: ScimCaller,
    user: User,
    changes: UserChanges,
    base_url: str,
) -> WriteResult:
    link = await _link_for(session, caller.organization_id, user)
    changed = await _apply(session, caller, user, link, changes)
    [resource] = await render(session, caller.organization_id, [user], base_url)
    return WriteResult(
        resource=resource,
        user=user,
        action=_action(changed),
        payload=caller.audit_payload(email=user.email, changes=changed),
    )


async def delete_org_scim_users(session: AsyncSession, org_id: uuid.UUID) -> None:
    """The organization purge: its user links (they would cascade; spelled out)."""
    await session.execute(
        delete(ScimUserLink)
        .where(ScimUserLink.organization_id == org_id)
        .execution_options(synchronize_session=False)
    )
