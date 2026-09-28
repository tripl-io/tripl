import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from tripl.models.domain_enums import ApiKeyScope, OrganizationRole

# The role vocabulary of the users API and ``/auth/me``: the ORGANIZATION role
# (owner | admin | member) since F20 PR4. ``users.role`` (owner | editor |
# viewer) is no longer read or returned; what a member may do inside a project
# is their project role.
Role = OrganizationRole

# Single source of truth for the password policy. Enforced authoritatively here
# (the schema is the only place a new password is validated before it is stored),
# and echoed verbatim to users on the register form and the change-password UI so
# the client hints can never drift from what the server accepts.
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 255
PASSWORD_POLICY_MESSAGE = (
    "Password must be at least 12 characters and include a number and a symbol."
)


def validate_password_strength(value: str) -> str:
    """Enforce the shared password policy at set-password time.

    Policy: at least ``PASSWORD_MIN_LENGTH`` characters, with at least one digit
    and one symbol (any non-alphanumeric, non-whitespace character). Applied on
    registration (and any future change-password path) — NOT on login, which stays
    lenient so accounts created under the old 8-character rule can still authenticate.
    """
    has_digit = any(char.isdigit() for char in value)
    # A whitespace char is not a "symbol" — the user-facing copy promises a real
    # punctuation/symbol character, so a trailing space must not satisfy the rule.
    has_symbol = any(not char.isalnum() and not char.isspace() for char in value)
    if len(value) < PASSWORD_MIN_LENGTH or not has_digit or not has_symbol:
        raise ValueError(PASSWORD_POLICY_MESSAGE)
    return value


class RegisterRequest(BaseModel):
    # EmailStr enforces RFC syntax + normalizes the address (IDN, casing) so
    # we don't accept e.g. "abc" past the old min_length=3 floor.
    email: EmailStr
    password: str = Field(max_length=PASSWORD_MAX_LENGTH)
    name: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("password")
    @classmethod
    def _enforce_password_policy(cls, value: str) -> str:
        return validate_password_strength(value)


class LoginRequest(BaseModel):
    # Lenient on purpose: the DB is the source of truth on login, so even an
    # already-stored "weird" email (legacy / pre-EmailStr) can still sign in.
    # Password stays lenient too — the policy is enforced at set-password time,
    # not here, so pre-policy accounts are not locked out.
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=255)


class AuthStatusResponse(BaseModel):
    # Unauthenticated bootstrap signal: lets the auth screen tell a brand-new
    # instance (no users yet) apart from a provisioned one.
    has_users: bool
    # Whether POST /auth/register would be accepted right now, so the auth screen
    # can hide the sign-up form instead of walking the visitor into a 403. This
    # is an instance-wide fact (identical for every caller), so exposing it
    # unauthenticated leaks nothing about individual accounts. True on an empty
    # instance regardless of policy — the first owner must always be able to
    # claim it.
    registration_enabled: bool = True
    # Whether the instance can send mail (SMTP host AND From: address), so the
    # forgot-password form can say up front that no email will come instead of
    # after the request (ST-24). Instance-wide, and already returned by the
    # unauthenticated reset request, so exposing it here leaks nothing new.
    email_configured: bool = False


class OrgMembershipOut(BaseModel):
    """One organization the signed-in user belongs to, with their role there."""

    slug: str
    name: str
    role: Role


class AuthUserResponse(BaseModel):
    """The signed-in account. Built by ``auth_service.build_auth_user_response``.

    ``role`` is the organization role in the organization the request acts in
    (``None`` when the user belongs to none that applies); ``orgs`` lists every
    membership. ``is_platform_admin`` is the operator flag, which grants the
    operator settings and nothing inside any organization.
    """

    id: uuid.UUID
    email: str
    name: str | None
    role: Role | None
    is_platform_admin: bool = False
    orgs: list[OrgMembershipOut] = Field(default_factory=list)
    # The slug of the organization this request acts in, when one is bound: an
    # API key's own organization. ``None`` for a browser session on
    # ``/auth/me``, which acts in no organization (F20 PR6, ``tripl whoami``).
    org: str | None = None
    # ``read`` or ``write`` when the caller authenticated with an API key;
    # ``None`` for a browser session.
    api_key_scope: ApiKeyScope | None = None
    created_at: datetime
    updated_at: datetime


class UserListItem(BaseModel):
    """A member of the request's organization, with their organization role.

    Built explicitly by ``user_service`` from the membership row, never
    validated from a ``User`` (whose ``role`` is the unread legacy column).
    """

    id: uuid.UUID
    email: str
    name: str | None
    role: Role
    created_at: datetime


class UserRoleUpdate(BaseModel):
    """``PATCH /users/{id}``: the target's new ORGANIZATION role.

    The vocabulary is the organization's: ``owner``, ``admin`` or ``member``.
    The instance-era values map as ``owner`` -> ``owner`` and ``editor`` /
    ``viewer`` -> ``member`` (write rights inside a project are the project
    role's business); they are not accepted here any more (422).
    """

    role: Role
