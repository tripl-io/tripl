"""A multi-tenant instance for Community tests. Not a test module.

Community ships no multi-tenant policy (``tripl.tenancy``; tripl Enterprise
does). What stays in Community still branches on one: the default organization
is not the operator scope, an org-less URL acts in the user's only
organization, no first-account bootstrap, verification required, no local
photo storage for organizations, a From address needs the organization's own
relay. Tests of those install this stand-in. Sign-up that creates an
organization, and who may create one, are the extension's: not here.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import tenancy
from tripl.config import DEPLOYMENT_HOSTED, settings
from tripl.middleware.org_context import OrgRef
from tripl.models.user import User
from tripl.services import org_resolution


class MultiTenantForTests(tenancy.TenancyPolicy):
    multi_tenant = True

    async def orgless_org(self, session: AsyncSession, user: User) -> OrgRef:
        return await org_resolution.only_org_of(session, user.id)


POLICY = MultiTenantForTests()


def use_multi_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as a hosted instance: ``DEPLOYMENT_MODE=hosted`` and the stand-in policy."""
    monkeypatch.setattr(settings, "deployment_mode", DEPLOYMENT_HOSTED)
    monkeypatch.setattr(tenancy, "policy", lambda: POLICY)
