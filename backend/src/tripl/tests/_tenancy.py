"""A multi-tenant instance for Community tests. Not a test module.

Community ships no multi-tenant policy (``tripl.tenancy``; tripl Enterprise
does). What stays in Community still branches on one: the default organization
is not the operator scope, an org-less URL acts in the user's only
organization, no first-account bootstrap, verification required, no local
photo storage for organizations, a From address needs the organization's own
relay. Tests of those install this stand-in. Sign-up that creates an
organization, and who may create one, are the extension's: not here.

Community runs one organization: ``POST /orgs`` is the Enterprise edition's.
The organization model and its isolation stay in core, so tests that build
several organizations through the API install :func:`use_multi_org`.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import tenancy
from tripl.config import DEPLOYMENT_HOSTED, settings
from tripl.middleware.org_context import OrgRef
from tripl.models.user import User
from tripl.services import org_resolution


class MultiOrgForTests(tenancy.TenancyPolicy):
    """One team's instance that creates more organizations (as Enterprise does)."""

    multi_org = True


class MultiTenantForTests(tenancy.TenancyPolicy):
    multi_tenant = True
    multi_org = True

    async def orgless_org(self, session: AsyncSession, user: User) -> OrgRef:
        return await org_resolution.only_org_of(session, user.id)


POLICY = MultiTenantForTests()
MULTI_ORG = MultiOrgForTests()


def use_multi_org(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a platform admin create organizations, the deployment mode unchanged."""
    monkeypatch.setattr(tenancy, "policy", lambda: MULTI_ORG)


def use_multi_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as a hosted instance: ``DEPLOYMENT_MODE=hosted`` and the stand-in policy."""
    monkeypatch.setattr(settings, "deployment_mode", DEPLOYMENT_HOSTED)
    monkeypatch.setattr(tenancy, "policy", lambda: POLICY)


class PublicDemoForTests(tenancy.TenancyPolicy):
    """A hosted public demo, as the Enterprise edition runs one (``PUBLIC_DEMO``)."""

    multi_tenant = True
    multi_org = True

    @property
    def public_demo(self) -> bool:
        return True

    async def orgless_org(self, session: AsyncSession, user: User) -> OrgRef:
        return await org_resolution.only_org_of(session, user.id)


PUBLIC_DEMO = PublicDemoForTests()


def use_public_demo(monkeypatch: pytest.MonkeyPatch, *, hosted: bool = False) -> None:
    """Run as a public demo (refusals on); ``hosted`` also makes it multi-tenant."""
    if hosted:
        monkeypatch.setattr(settings, "deployment_mode", DEPLOYMENT_HOSTED)

    class _SingleTeamDemo(tenancy.TenancyPolicy):
        @property
        def public_demo(self) -> bool:
            return True

    chosen = PUBLIC_DEMO if hosted else _SingleTeamDemo()
    monkeypatch.setattr(tenancy, "policy", lambda: chosen)


def use_single_team(monkeypatch: pytest.MonkeyPatch) -> None:
    """Back to Community's own policy (no public demo, one organization)."""
    monkeypatch.setattr(tenancy, "policy", lambda: tenancy.TenancyPolicy())
