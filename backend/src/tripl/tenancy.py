"""Who an instance serves: one team, or many tenants (a multi-tenant service).

Every decision that differs between the two is answered here. The base
:class:`TenancyPolicy` is the single-team answer, the one Community ships. A
multi-tenant service is an extension's: its ``Extension.tenancy()`` returns a
policy of its own, and :func:`policy` answers with it. ``DEPLOYMENT_MODE=hosted``
asks for one; without an extension that supplies it the instance refuses to
start (:func:`check_deployment_mode`) rather than run hosted half-way.

Single team: everyone is in the default organization, whose settings are the
operator's; the first account of an empty instance owns it; sign-up joins it;
no email verification; no further organizations are created. What another
policy changes is what each member documents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tripl.config import DEPLOYMENT_HOSTED, settings

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from tripl.middleware.org_context import OrgRef
    from tripl.models.user import User
    from tripl.schemas.auth import RegisterRequest


#: The refusal of ``POST /orgs`` on a single-team instance.
MORE_ORGS_ARE_ENTERPRISE = "Creating more organizations is part of tripl Enterprise"


class TenancyPolicy:
    """The single-team instance. Subclass to serve many tenants."""

    #: Many tenants share the instance. Read by the branches that only differ
    #: in a yes/no: the default organization is not the operator scope, there
    #: is no first-account bootstrap, sign-up names a new organization, email
    #: verification is required, organizations may not keep photos on the
    #: server's disk, and a custom From address needs the organization's own
    #: relay.
    multi_tenant: bool = False

    #: More than one organization may be created (``POST /orgs``). A single-team
    #: instance has its default organization only; organizations it already
    #: has keep working.
    multi_org: bool = False

    #: A public demo: strangers sign in and explore generated demo projects, so
    #: whatever would reach outside the instance is refused (``api.deps.
    #: refuse_on_public_demo``) and the app says it is a demo. The Enterprise
    #: edition's (``PUBLIC_DEMO``); Community never runs one.
    @property
    def public_demo(self) -> bool:
        return False

    async def orgless_org(self, session: AsyncSession, user: User) -> OrgRef:
        """The organization a request acts in when its URL names none."""
        from tripl.services import org_resolution

        return await org_resolution.default_org_for(session, user)

    async def register(
        self, session: AsyncSession, data: RegisterRequest, *, email_can_send: bool
    ) -> tuple[User, str, str]:
        """A multi-tenant sign-up: ``(user, session_token, verification_token)``.

        Called only when :attr:`multi_tenant`; a single-team sign-up joins the
        default organization (``auth_service.register_user``).
        """
        raise NotImplementedError

    async def after_verified_sign_in(self, session: AsyncSession, user: User) -> None:
        """An account signed in through a provider that proved its address.

        No commit. A multi-tenant policy gives an account with no organization
        one of its own here.
        """

    async def require_org_creator(self, request: Request, user: User) -> User:
        """Who may create an organization (``POST /orgs``).

        A platform admin, and only where :attr:`multi_org`: on a single-team
        instance a platform admin is told it is the Enterprise edition's (403
        :data:`MORE_ORGS_ARE_ENTERPRISE`), anyone else is refused as before.
        """
        from fastapi import HTTPException, status

        from tripl.api.deps import require_platform_admin

        admin = await require_platform_admin(request, user)
        if not self.multi_org:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=MORE_ORGS_ARE_ENTERPRISE
            )
        return admin


_SINGLE_TEAM = TenancyPolicy()


def policy() -> TenancyPolicy:
    """The first extension's tenancy policy, else the single-team one."""
    from tripl import extensions

    for extension in extensions.extensions():
        supplied = extension.tenancy()
        if supplied is not None:
            return supplied
    return _SINGLE_TEAM


def multi_tenant() -> bool:
    return policy().multi_tenant


def public_demo() -> bool:
    """Whether this instance is a public demo (:attr:`TenancyPolicy.public_demo`)."""
    return policy().public_demo


def check_deployment_mode() -> None:
    """Refuse ``DEPLOYMENT_MODE=hosted`` when no extension serves it."""
    if settings.deployment_mode == DEPLOYMENT_HOSTED and not multi_tenant():
        raise RuntimeError(
            "DEPLOYMENT_MODE=hosted needs an extension that runs a multi-tenant "
            "service (tripl Enterprise), and this build has none. Unset it or "
            "set DEPLOYMENT_MODE=self_hosted."
        )
