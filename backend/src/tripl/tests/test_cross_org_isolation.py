"""Two organizations with the same names never see each other (F20 PR5, GH #273).

Organizations are isolated by application code, not by row-level security, so
this module is the net under that code. Two organizations, ``org-alpha`` (A)
and ``org-bravo`` (B), each get the same-named things — a project ``web``, a
data source ``warehouse`` — plus an owner, a member, an API key and a plan built
through the API: event types, fields, relations, events, comments, a photo,
variables, meta fields, annotations, branches, revisions, docs, alert
destinations and rules, an inbox incident, an anomaly, a scan, a metric, a fact
table, an invitation, a notification. Everything A owns that is not a shared
name carries the marker ``alpha``; nothing of B does.

Then every route of the live app (walked with ``test_rbac.iter_api_routes``,
so a new route is matrixed the day it lands) is driven by B's actors:

* under ``/api/v1/orgs/org-alpha/...`` with A's ids: ``404`` for B's owner, B's
  member and B's API key, whatever the route — the organization itself is
  unknown to them, the same answer as for an organization that does not exist;
* under the legacy path (which acts in B) with A's ids: never ``2xx``, and the
  same status and body as the same request with fresh random ids, so the answer
  is no oracle for A's rows; a legacy ``GET`` that names no id reads B's own
  ``web`` and must not carry any A id or the marker;
* B's lists (projects, data sources, users, invitations, audit, activity, API
  keys, notifications, search, docs tree) carry nothing of A, for the bridge
  user who belongs to both organizations too;
* A's ids smuggled in request BODIES of B's own project are refused or ignored.

Routes that are not per-organization are allowlisted with a reason; a coverage
guard fails when a route is in neither the matrix nor the allowlist.

Why every test seeds its own pair instead of one module fixture: the suite runs
under ``pytest-xdist --dist worksteal``, which may interleave another module's
tests on the same worker, and ``setup_db`` empties every table after each test.
A module-scoped seed would be wiped between two of these tests.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, select

from tripl.config import settings
from tripl.main import app
from tripl.middleware.org_context import ORG_REWRITE_PREFIXES
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.audit_log import AuditLog
from tripl.models.domain_enums import ProjectGenerationStatus
from tripl.models.event_photo import EventPhoto
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.notification import Notification
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.project import Project
from tripl.tests._accounts import sign_up
from tripl.tests._incident_summary_seed import SeededIncident, add_item
from tripl.tests._members import add_member, add_org_member
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_rbac import iter_api_routes

PASSWORD = "Password123!"
API = "/api/v1"
ORG_A_ID = uuid.UUID("00000000-0000-0000-0000-0000000a1fa0")
ORG_B_ID = uuid.UUID("00000000-0000-0000-0000-0000000b7a70")
ORG_A = "org-alpha"
ORG_B = "org-bravo"
SLUG = "web"
SOURCE_NAME = "warehouse"
#: A project slug only A holds.
A_ONLY_SLUG = "alpha-shop"
#: In every name A gives a row that is not deliberately shared with B.
MARKER = "alpha"
BUCKET = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)

# ── what is not per organization ─────────────────────────────────────────────

_AUTH_REASON = (
    "identity, not tenancy: acts on the caller's own account or on a token, has "
    "no org-qualified form (auth is not in ORG_REWRITE_PREFIXES) and resolves no "
    "project slug"
)
_SETTINGS_REASON = (
    "legacy combined settings view (F20 PR9): operator fields gated to platform "
    "admins; organization fields act in the caller's own resolved organization "
    "(deps.legacy_settings_org_id, never another), whose org-qualified form is "
    "/orgs/{org}/settings, driven by the matrix; test_org_settings.py pins that "
    "a hosted caller's legacy write lands in their own organization only"
)
_PLATFORM_REASON = (
    "the operator console (F20 PR9): the operator scope, platform admins only "
    "(require_platform_admin); names and reads no organization"
)
_PLATFORM_CONSOLE_REASON = (
    "the platform console (F20 PR14): platform admins only, browser session "
    "(require_platform_admin); org-free, it names an organization by slug as its "
    "SUBJECT (metadata, suspension, step-in), never acts in one; "
    "test_platform_console.py and test_platform_step_in.py pin who reaches it"
)

#: Every route that is NOT per organization, with the reason. Keyed by path: a
#: path's methods share the reason.
PUBLIC_OR_INSTANCE_WIDE: dict[str, str] = {
    "/health": "liveness probe, unauthenticated, reads no tenant data",
    f"{API}/auth/status": _AUTH_REASON,
    f"{API}/auth/register": _AUTH_REASON,
    f"{API}/auth/invitations/{{token}}": (
        "an invitation is addressed by its secret single-use token, which is its "
        "own authorization; unauthenticated by design"
    ),
    f"{API}/auth/invitations/{{token}}/accept": (
        "redeems the secret single-use token for the organization that issued it"
    ),
    f"{API}/auth/login": _AUTH_REASON,
    f"{API}/auth/password-reset/request": _AUTH_REASON,
    f"{API}/auth/password-reset/confirm": _AUTH_REASON,
    f"{API}/auth/verify-email/request": _AUTH_REASON,
    f"{API}/auth/verify-email/confirm": _AUTH_REASON,
    f"{API}/auth/logout": _AUTH_REASON,
    f"{API}/auth/google/start": (
        "Sign in with Google: the instance's own OAuth client, unauthenticated, "
        "acts in no organization; test_google_sign_in.py pins the flow"
    ),
    f"{API}/auth/google/callback": (
        "Sign in with Google: Google's redirect back, authorized by the "
        "encrypted state cookie bound to the browser; test_google_sign_in.py pins the flow"
    ),
    f"{API}/auth/me": _AUTH_REASON,
    f"{API}/settings": _SETTINGS_REASON,
    f"{API}/settings/photo-limits": _SETTINGS_REASON,
    f"{API}/settings/row-limits": _SETTINGS_REASON,
    f"{API}/settings/ai/defaults": _SETTINGS_REASON,
    f"{API}/settings/ai": _SETTINGS_REASON,
    f"{API}/settings/ai/test": _SETTINGS_REASON,
    f"{API}/settings/email/test": _SETTINGS_REASON,
    f"{API}/platform/settings": _PLATFORM_REASON,
    f"{API}/platform/settings/ai/test": _PLATFORM_REASON,
    f"{API}/platform/settings/email/test": _PLATFORM_REASON,
    f"{API}/platform/orgs": _PLATFORM_CONSOLE_REASON,
    f"{API}/platform/orgs/{{org_slug}}": _PLATFORM_CONSOLE_REASON,
    f"{API}/platform/orgs/{{org_slug}}/suspend": _PLATFORM_CONSOLE_REASON,
    f"{API}/platform/orgs/{{org_slug}}/unsuspend": _PLATFORM_CONSOLE_REASON,
    f"{API}/platform/orgs/{{org_slug}}/step-in": _PLATFORM_CONSOLE_REASON,
    f"{API}/platform/users": _PLATFORM_CONSOLE_REASON,
    f"{API}/platform/users/{{user_id}}/platform-admin": _PLATFORM_CONSOLE_REASON,
    f"{API}/platform/step-ins": _PLATFORM_CONSOLE_REASON,
    f"{API}/platform/step-ins/{{step_in_id}}/end": _PLATFORM_CONSOLE_REASON,
    f"{API}/project-templates": "static instance-wide catalog of starter templates",
    f"{API}/orgs": (
        "the caller's own organizations (an API key: its own one) and creating a new "
        "one (a platform admin self-hosted, any verified session hosted); names no "
        "organization in the path"
    ),
}

#: Legacy-path routes the matrix does not drive, with the reason. Their
#: org-qualified form is still driven (and must 404).
LEGACY_NOT_DRIVEN: dict[tuple[str, str], str] = {
    ("GET", f"{API}/projects/{{slug}}/events/stream"): (
        "server-sent events: on B's own ``web`` the stream never ends; the "
        "org-qualified row proves the fence, and the stream resolves the slug "
        "through the same membership gate"
    ),
}

#: Path parameters the seed cannot create a real A row for through the API or a
#: plain ORM row. The route is still driven with a fresh id, so it still proves
#: that the org-qualified form 404s and the legacy form answers like an unknown
#: id — but not against a live A row.
UNSEEDED_PARAMS: dict[str, str] = {
    "candidate_id": "shadow-event candidates come from a warehouse scan",
    "drift_id": "schema and variable drifts come from a warehouse scan",
    "job_id": "scan, preview and dry-run jobs are Celery jobs",
    "override_id": "anomaly scope overrides are written by the detector",
    "resolution_id": "conflict resolutions need a conflicting branch merge",
    "domain_id": "SSO domains are owner-only rows the seed does not claim",
    "token_id": "SCIM tokens are owner-only rows the seed does not mint",
}

#: Id-less legacy GETs whose REQUIRED query names one of A's rows (see
#: ``_query``). In B that row does not exist, so a 404 is the right answer and
#: not a sign that the route never reached B; every other id-less GET must be
#: 2xx for B's owner.
QUERY_NAMES_AN_A_ROW: tuple[str, ...] = (
    "/docs/file",
    # A's note does not exist in B: 404. (Folder sharing answers 200 to B's
    # owner — an org owner/admin sees any folder path — so it needs no entry.)
    "/docs/file/sharing",
    "/docs/revisions",
    "/docs/translations/revisions",
    "/distribution-drifts",
)

#: Every path parameter the matrix knows how to fill. A new one fails the
#: coverage guard until someone decides what A row it should name.
KNOWN_PARAMS = frozenset(
    {
        "slug",
        "annotation_id",
        "anomaly_id",
        "branch_id",
        "comment_id",
        "correlation_group_id",
        "delivery_id",
        "destination_id",
        "ds_id",
        "entity_id",
        "entity_type",
        "entry_id",
        "event_id",
        "event_type_id",
        "fact_table_id",
        "field_id",
        "group_id",
        "invitation_id",
        "key_id",
        "org",
        "meta_field_id",
        "metric_id",
        "owner_id",
        "photo_id",
        "planned_event_id",
        "relation_id",
        "revision_id",
        "rule_id",
        "scan_config_id",
        "scan_id",
        "user_id",
        "variable_id",
        *UNSEEDED_PARAMS,
    }
)

_PARAM = re.compile(r"{(\w+)}")
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: A 1x1 PNG.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _matrix_routes() -> list[tuple[str, str]]:
    """``(method, path)`` for every route that is per organization."""
    out: list[tuple[str, str]] = []
    for path, route in iter_api_routes():
        if path in PUBLIC_OR_INSTANCE_WIDE:
            continue
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            out.append((method, path))
    return out


def _org_rewritable(path: str) -> bool:
    head = path.removeprefix(f"{API}/").split("/", 1)[0]
    return path.startswith(f"{API}/") and head in ORG_REWRITE_PREFIXES


def _org_addressed(path: str) -> bool:
    """A real ``/orgs/{org}/...`` route (F20 PR6): the organization IS the path.

    It has no legacy form; the org-qualified matrix drives it with ``{org}`` set
    to A, where it must 404 for B's actors like every other route.
    """
    return path == f"{API}/orgs/{{org}}" or path.startswith(f"{API}/orgs/{{org}}/")


# ── the two organizations ───────────────────────────────────────────────────


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@dataclass
class Actor:
    """Someone of org B, with the prefix that acts in B for them."""

    name: str
    client: AsyncClient
    #: ``/api/v1`` for a caller whose legacy path resolves to B; the org-qualified
    #: B prefix for the bridge user, whose legacy path is ambiguous (two orgs).
    own_prefix: str = API
    headers: dict[str, str] = field(default_factory=dict)

    async def call(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        body: Any = None,
    ) -> Response:
        kwargs: dict[str, Any] = {"headers": self.headers, "params": params}
        if method == "POST" and url.endswith("/photos"):
            # The one multipart route: an upload, so it reaches its handler.
            kwargs["files"] = {"file": ("probe.png", _PNG, "image/png")}
        elif method in _MUTATING:
            kwargs["json"] = {} if body is None else body
        return await self.client.request(method, url, **kwargs)


@dataclass
class OrgSeed:
    """One organization's people and the ids of what they built."""

    slug: str
    org_id: uuid.UUID
    owner: AsyncClient
    member: AsyncClient
    key_client: AsyncClient
    owner_id: str = ""
    member_id: str = ""
    key_token: str = ""
    ids: dict[str, str] = field(default_factory=dict)
    doc_path: str = ""
    org_doc_path: str = ""


@dataclass
class World:
    a: OrgSeed
    b: OrgSeed
    bridge: AsyncClient
    bridge_id: str
    notification_id: str

    def b_actors(self, *, with_bridge: bool) -> list[Actor]:
        actors = [
            Actor("b-owner", self.b.owner),
            Actor("b-member", self.b.member),
            Actor(
                "b-api-key",
                self.b.key_client,
                headers={"Authorization": f"Bearer {self.b.key_token}"},
            ),
        ]
        if with_bridge:
            actors.append(Actor("bridge", self.bridge, own_prefix=f"{API}/orgs/{ORG_B}"))
        return actors

    def a_markers(self) -> list[str]:
        """What must never appear in anything B reads."""
        values = {MARKER, ORG_A, self.a.owner_id, self.a.member_id, self.notification_id}
        values.update(v for k, v in self.a.ids.items() if k != "entity_type")
        return sorted(v for v in values if v)


async def _ok(resp: Response, *codes: int) -> Any:
    assert resp.status_code in (codes or (200, 201)), f"{resp.request.url}: {resp.text}"
    return resp.json() if resp.content else None


async def _register(client: AsyncClient, email: str, name: str) -> str:
    # Hosted here: a verified default-org member, as hosted sign-up used to make.
    return str(await sign_up(client, email=email, password=PASSWORD, name=name))


async def _move_to_org(user_id: str, org_id: uuid.UUID, role: str) -> None:
    """Registration put the user in the default organization; move them."""
    async with TestSessionLocal() as session:
        await session.execute(
            delete(OrganizationMember).where(
                OrganizationMember.user_id == uuid.UUID(user_id),
                OrganizationMember.organization_id == DEFAULT_ORG_ID,
            )
        )
        await session.commit()
        await add_org_member(session, uuid.UUID(user_id), role, org_id=org_id)


async def _seed_plan(seed: OrgSeed, p: str) -> None:
    """Build org ``seed``'s project ``web`` through the API (owner's session).

    ``p`` prefixes every name that is not deliberately shared between A and B.
    """
    c = seed.owner
    base = f"{API}/projects/{SLUG}"
    ids = seed.ids

    project = await _ok(await c.post(f"{API}/projects", json={"name": "Web", "slug": SLUG}))
    ids["project_id"] = project["id"]
    await _ok(await c.post(f"{base}/members", json={"user_id": seed.member_id, "role": "editor"}))
    source = await _ok(
        await c.post(
            f"{API}/data-sources",
            json={
                "name": SOURCE_NAME,
                "db_type": "clickhouse",
                # Nothing listens there: a warehouse call fails at once instead
                # of waiting out a connect timeout.
                "host": "localhost",
                "port": 1,
                "database_name": "analytics",
                "username": "default",
                "password": f"{p}-secret",
            },
        )
    )
    ids["ds_id"] = source["id"]

    checkout = await _ok(
        await c.post(
            f"{base}/event-types", json={"name": f"{p}_checkout", "display_name": f"{p} Checkout"}
        )
    )
    signup = await _ok(
        await c.post(
            f"{base}/event-types", json={"name": f"{p}_signup", "display_name": f"{p} Signup"}
        )
    )
    ids["event_type_id"] = checkout["id"]
    amount = await _ok(
        await c.post(
            f"{base}/event-types/{checkout['id']}/fields",
            json={"name": f"{p}_amount", "display_name": f"{p} Amount", "field_type": "string"},
        )
    )
    user_ref = await _ok(
        await c.post(
            f"{base}/event-types/{signup['id']}/fields",
            json={"name": f"{p}_user", "display_name": f"{p} User", "field_type": "string"},
        )
    )
    ids["field_id"] = amount["id"]
    relation = await _ok(
        await c.post(
            f"{base}/relations",
            json={
                "source_event_type_id": checkout["id"],
                "target_event_type_id": signup["id"],
                "source_field_id": amount["id"],
                "target_field_id": user_ref["id"],
            },
        )
    )
    ids["relation_id"] = relation["id"]
    owner_row = await _ok(
        await c.post(
            f"{base}/event-types/{checkout['id']}/owners", json={"user_id": seed.member_id}
        )
    )
    ids["owner_id"] = owner_row["id"]

    event = await _ok(
        await c.post(
            f"{base}/events",
            json={
                "event_type_id": checkout["id"],
                "name": f"{p}_purchase",
                "status": "implemented",
            },
        )
    )
    await _ok(
        await c.post(f"{base}/events", json={"event_type_id": signup["id"], "name": f"{p}_joined"})
    )
    ids["event_id"] = event["id"]
    ids["entity_id"] = event["id"]
    ids["entity_type"] = "event"
    comment = await _ok(
        await c.post(f"{base}/events/{event['id']}/comments", json={"body": f"{p} question"})
    )
    ids["event_comment_id"] = comment["id"]
    await _ok(await c.put(f"{base}/subscriptions/event/{event['id']}", json={}), 200, 201, 204)

    meta = await _ok(
        await c.post(
            f"{base}/meta-fields",
            json={"name": f"{p}_team", "display_name": f"{p} Team", "field_type": "string"},
        )
    )
    ids["meta_field_id"] = meta["id"]
    variable = await _ok(await c.post(f"{base}/variables", json={"name": f"{p}_plan"}))
    ids["variable_id"] = variable["id"]
    annotation = await _ok(
        await c.post(
            f"{base}/annotations", json={"bucket": "2026-05-01T10:00:00Z", "label": f"{p} deploy"}
        )
    )
    ids["annotation_id"] = annotation["id"]
    planned = await _ok(
        await c.post(
            f"{base}/planned-events",
            json={
                "label": f"{p} sale",
                "starts_at": "2026-05-01T00:00:00Z",
                "ends_at": "2026-05-02T00:00:00Z",
            },
        )
    )
    ids["planned_event_id"] = planned["id"]

    revision = await _ok(await c.post(f"{base}/revisions", json={"summary": f"{p} snapshot"}))
    ids["plan_revision_id"] = revision["id"]
    branch = await _ok(await c.post(f"{base}/branches", json={"name": f"{p}-feature"}))
    ids["branch_id"] = branch["id"]
    branch_comment = await _ok(
        await c.post(f"{base}/branches/{branch['id']}/comments", json={"body": f"{p} review note"})
    )
    ids["branch_comment_id"] = branch_comment["id"]
    await _ok(
        await c.post(f"{base}/branches/{branch['id']}/reviewers", json={"user_id": seed.member_id})
    )

    seed.doc_path = f"{p}/notes.md"
    seed.org_doc_path = f"{p}/org-notes.md"
    await _ok(
        await c.put(
            f"{base}/docs/file",
            params={"scope": "project", "path": seed.doc_path},
            json={"content": f"# {p} notes\n\n{p} secret note"},
        )
    )
    await _ok(
        await c.put(
            f"{base}/docs/file",
            params={"scope": "organization", "path": seed.org_doc_path},
            json={"content": f"# {p} org notes\n\n{p} org secret"},
        )
    )
    doc_revisions = await _ok(
        await c.get(f"{base}/docs/revisions", params={"scope": "project", "path": seed.doc_path})
    )
    ids["doc_revision_id"] = doc_revisions["items"][0]["id"]

    destination = await _ok(
        await c.post(
            f"{base}/alert-destinations",
            json={
                "type": "slack",
                "name": f"{p} slack",
                "webhook_url": f"https://hooks.slack.com/services/T1/B1/{p}{uuid.uuid4().hex[:6]}",
            },
        )
    )
    ids["destination_id"] = destination["id"]
    rule = await _ok(
        await c.post(
            f"{base}/alert-destinations/{destination['id']}/rules", json={"name": f"{p} rule"}
        )
    )
    ids["rule_id"] = rule["id"]

    scan = await _ok(
        await c.post(
            f"{base}/scans",
            json={"data_source_id": source["id"], "name": f"{p} scan", "base_query": "SELECT 1"},
        )
    )
    ids["scan_id"] = scan["id"]
    ids["scan_config_id"] = scan["id"]
    metric = await _ok(
        await c.post(
            f"{base}/metrics",
            json={
                "kind": "sql",
                "name": f"{p}_metric",
                "display_name": f"{p} Metric",
                "data_source_id": source["id"],
                "interval": "1d",
                "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
            },
        )
    )
    ids["metric_id"] = metric["id"]
    fact_table = await _ok(
        await c.post(
            f"{base}/fact-tables",
            json={
                "name": f"{p}_orders",
                "display_name": f"{p} Orders",
                "sql": "SELECT created_at, amount FROM orders",
                "timestamp_column": "created_at",
                "data_source_id": source["id"],
                "columns": [
                    {"name": "created_at", "type": "timestamp"},
                    {"name": "amount", "type": "number"},
                ],
            },
        )
    )
    ids["fact_table_id"] = fact_table["id"]

    invitation = await _ok(
        await c.post(f"{API}/users/invitations", json={"email": f"{p}-invitee@example.com"})
    )
    ids["invitation_id"] = (invitation.get("invitation") or invitation)["id"]
    key = await _ok(await c.post(f"{API}/me/api-keys", json={"name": f"{p} key", "scope": "write"}))
    ids["key_id"] = key["id"]
    seed.key_token = key["token"]
    group = await _ok(
        await c.post(f"{API}/orgs/{seed.slug}/groups", json={"name": f"{p} team"}), 201
    )
    ids["group_id"] = group["id"]
    await _ok(
        await c.post(
            f"{API}/orgs/{seed.slug}/groups/{group['id']}/members",
            json={"user_id": seed.member_id},
        ),
        201,
    )

    await _seed_rows(seed, p)

    audit = await _ok(await c.get(f"{API}/audit"))
    ids["entry_id"] = audit["items"][0]["id"]


async def _seed_rows(seed: OrgSeed, p: str) -> None:
    """What the API cannot make without a warehouse: an incident, an anomaly, a photo."""
    ids = seed.ids
    group_id = uuid.uuid4()
    incident = SeededIncident(
        slug=SLUG,
        project_id=uuid.UUID(ids["project_id"]),
        event_type_id=uuid.UUID(ids["event_type_id"]),
        event_id=uuid.UUID(ids["event_id"]),
        scan_config_id=uuid.UUID(ids["scan_id"]),
        destination_id=uuid.UUID(ids["destination_id"]),
        rule_id=uuid.UUID(ids["rule_id"]),
        group_id=group_id,
    )
    await add_item(incident, bucket=BUCKET, sample_value=f"{p}-sample")
    ids["correlation_group_id"] = str(group_id)
    async with TestSessionLocal() as session:
        delivery_id = await session.scalar(
            select(AlertDelivery.id).where(AlertDelivery.destination_id == incident.destination_id)
        )
        ids["delivery_id"] = str(delivery_id)
        anomaly = MetricAnomaly(
            scan_config_id=incident.scan_config_id,
            scope_type="event",
            scope_ref=ids["event_id"],
            event_id=incident.event_id,
            event_type_id=None,
            bucket=BUCKET,
            actual_count=30,
            expected_count=120,
            stddev=10,
            z_score=-9,
            direction="drop",
            created_at=BUCKET,
        )
        photo = EventPhoto(
            project_id=incident.project_id,
            event_id=incident.event_id,
            original_filename=f"{p}-mock.png",
            kind="figma",
            external_url=f"https://www.figma.com/file/{p}mock",
        )
        session.add_all([anomaly, photo])
        await session.commit()
        ids["anomaly_id"] = str(anomaly.id)
        ids["photo_id"] = str(photo.id)
    photo_comment = await _ok(
        await seed.owner.post(
            f"{API}/projects/{SLUG}/events/{ids['event_id']}/photos/{ids['photo_id']}/comments",
            json={"body": f"{p} pixel note"},
        )
    )
    ids["photo_comment_id"] = photo_comment["id"]


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    # Hosted: a session's legacy path acts in the user's ONLY organization, so
    # B's actors on ``/api/v1/...`` act in B, exactly as a hosted tenant does.
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    async with TestSessionLocal() as session:
        session.add_all(
            [
                Organization(id=ORG_A_ID, slug=ORG_A, name="Org Alpha"),
                Organization(id=ORG_B_ID, slug=ORG_B, name="Org Bravo"),
            ]
        )
        await session.commit()

    clients: list[AsyncClient] = [_new_client() for _ in range(7)]
    a_owner, a_member, b_owner, b_member, bridge, a_key, b_key = clients
    a = OrgSeed(slug=ORG_A, org_id=ORG_A_ID, owner=a_owner, member=a_member, key_client=a_key)
    b = OrgSeed(slug=ORG_B, org_id=ORG_B_ID, owner=b_owner, member=b_member, key_client=b_key)
    try:
        a.owner_id = await _register(a_owner, "alpha-owner@example.com", "Alpha Owner")
        a.member_id = await _register(a_member, "alpha-member@example.com", "Alpha Member")
        b.owner_id = await _register(b_owner, "bravo-owner@example.com", "Bravo Owner")
        b.member_id = await _register(b_member, "bravo-member@example.com", "Bravo Member")
        bridge_id = await _register(bridge, "bridge@example.com", "Bridge")
        await _move_to_org(a.owner_id, ORG_A_ID, "owner")
        await _move_to_org(a.member_id, ORG_A_ID, "member")
        await _move_to_org(b.owner_id, ORG_B_ID, "owner")
        await _move_to_org(b.member_id, ORG_B_ID, "member")
        await _move_to_org(bridge_id, ORG_A_ID, "member")
        async with TestSessionLocal() as session:
            await add_org_member(session, uuid.UUID(bridge_id), "member", org_id=ORG_B_ID)

        await _seed_plan(a, MARKER)
        await _seed_plan(b, "bravo")
        # A slug only A holds: B's legacy lookups of it must answer like any
        # unknown slug, which the shared ``web`` alone cannot show.
        await _ok(
            await a_owner.post(f"{API}/projects", json={"name": "Alpha Shop", "slug": A_ONLY_SLUG})
        )

        async with TestSessionLocal() as session:
            # The bridge is on both ``web`` projects, and A has notified them.
            for seed in (a, b):
                await add_member(
                    session,
                    uuid.UUID(seed.ids["project_id"]),
                    uuid.UUID(bridge_id),
                    "editor",
                    commit=False,
                )
            notification = Notification(
                user_id=uuid.UUID(bridge_id),
                project_id=uuid.UUID(a.ids["project_id"]),
                kind="comment",
                entity_type="event",
                entity_id=uuid.UUID(a.ids["event_id"]),
                title=f"{MARKER} commented",
                body=f"{MARKER} question",
                url=f"/p/{SLUG}/events/{a.ids['event_id']}",
                actor_user_id=uuid.UUID(a.owner_id),
            )
            session.add(notification)
            await session.commit()
            notification_id = str(notification.id)

        yield World(a=a, b=b, bridge=bridge, bridge_id=bridge_id, notification_id=notification_id)
    finally:
        for client in clients:
            await client.aclose()


# ── filling a route with A's ids ─────────────────────────────────────────────


def _a_value(name: str, path: str, w: World) -> str:
    """The A row a path parameter names, by the route it appears in."""
    ids = w.a.ids
    if name == "slug":
        return SLUG
    if name == "org":
        return ORG_A
    if name == "comment_id":
        if "/branches/" in path:
            return ids["branch_comment_id"]
        if "/photos/" in path:
            return ids["photo_comment_id"]
        return ids["event_comment_id"]
    if name == "revision_id":
        return ids["doc_revision_id"] if "/docs/" in path else ids["plan_revision_id"]
    if name == "user_id":
        return w.a.member_id
    if name in UNSEEDED_PARAMS:
        return str(uuid.uuid4())
    return ids[name]


def _fill(path: str, value: Callable[[str], str]) -> str:
    return _PARAM.sub(lambda m: value(m.group(1)), path)


def _a_url(path: str, w: World) -> str:
    return _fill(path, lambda name: _a_value(name, path, w))


def _control_url(path: str) -> str:
    """The same route with every id fresh: what an unknown row answers."""
    return _fill(
        path,
        lambda name: (
            SLUG if name == "slug" else "event" if name == "entity_type" else str(uuid.uuid4())
        ),
    )


def _query(path: str, w: World) -> dict[str, str] | None:
    """Required query parameters, naming A's rows where they can."""
    if path.endswith(("/docs/file", "/docs/revisions", "/docs/file/sharing")):
        return {"scope": "project", "path": w.a.doc_path}
    if path.endswith("/docs/translations/revisions"):
        return {"scope": "project", "path": w.a.doc_path, "lang": "de"}
    if path.endswith("/docs/translations/revisions/{revision_id}"):
        return {"scope": "project", "path": w.a.doc_path}
    if path.endswith(("/docs/folder", "/docs/folder/sharing")):
        return {"scope": "project", "path": MARKER}
    if path.endswith("/docs/export"):
        return {"scope": "organization"}
    if path.endswith("/docs/backlinks"):
        return {"kind": "event", "name": f"{MARKER}_purchase"}
    if path.endswith("/docs/links"):
        return {"ref": f"event:{MARKER}_purchase"}
    if path.endswith("/dependencies"):
        return {"entity": f"event:{w.a.ids['event_id']}"}
    if path.endswith("/health/events"):
        return {"ids": w.a.ids["event_id"]}
    if path.endswith("/events/by-names"):
        return {"event_type_id": w.a.ids["event_type_id"], "names": f"{MARKER}_purchase"}
    if path.endswith("/search") or path.endswith("/docs/search"):
        return {"q": MARKER}
    if path.endswith("/docs/link-suggestions"):
        return {"q": MARKER}
    if path.endswith(
        ("/top-movers", "/seasonality", "/breakdown-timeline", "/distribution-drifts")
    ):
        return {"scope_type": "event", "scope_ref": w.a.ids["event_id"]}
    if path.endswith("/revisions/{revision_id}/diff"):
        return {"compare_to": w.a.ids["plan_revision_id"]}
    return None


def _body(method: str, path: str, w: World) -> Any:
    """A body that passes validation, so an id route reaches its handler.

    An empty body would answer 422 before any lookup and prove nothing; these
    are just valid enough to get past the schema. Ids in them are A's.
    """
    if method not in _MUTATING:
        return None
    a = w.a.ids
    later = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    p = f"{API}/projects/{{slug}}"
    table: dict[str, Any] = {
        f"{p}/members/{{user_id}}": {"role": "viewer"},
        f"{API}/users/{{user_id}}": {"role": "member"},
        f"{p}/alert-destinations/{{destination_id}}/rules": {"name": "probe"},
        f"{p}/monitors/{{rule_id}}/mute": {"muted_until": later},
        f"{p}/alert-inbox/{{correlation_group_id}}/actions": {"action": "acknowledge"},
        f"{p}/event-types/drifts/{{drift_id}}/actions": {"action": "accept"},
        f"{p}/variables/drifts/{{drift_id}}/action": {"action": "accept"},
        f"{p}/event-types/{{event_type_id}}/owners": {"user_id": w.a.member_id},
        f"{p}/event-types/{{event_type_id}}/fields": {
            "name": "probe",
            "display_name": "Probe",
            "field_type": "string",
        },
        f"{p}/event-types/{{event_type_id}}/fields/bulk": {
            "fields": [{"name": "probe", "display_name": "Probe", "field_type": "string"}]
        },
        f"{p}/event-types/{{event_type_id}}/fields/reorder": {"field_ids": [a["field_id"]]},
        f"{p}/events/{{event_id}}/move": {"direction": "up"},
        f"{p}/metrics/{{metric_id}}/move": {"direction": "up"},
        f"{p}/events/{{event_id}}/photos/reorder": {"photo_ids": [a["photo_id"]]},
        f"{p}/events/{{event_id}}/photos/figma": {"url": "https://www.figma.com/file/probe"},
        f"{p}/events/{{event_id}}/photos/{{photo_id}}/comments": {"body": "probe"},
        f"{p}/events/{{event_id}}/comments": {"body": "probe"},
        f"{p}/events/{{event_id}}/comments/{{comment_id}}/actions": {"action": "resolve"},
        f"{p}/variables/{{variable_id}}/event-overrides/{{event_id}}": {"values": ["probe"]},
        f"{p}/scans/{{scan_id}}/metrics/replay": {
            "time_from": (BUCKET - timedelta(days=1)).isoformat(),
            "time_to": BUCKET.isoformat(),
        },
        f"{p}/branches/{{branch_id}}/transition": {"action": "close"},
        f"{p}/branches/{{branch_id}}/reviewers": {"user_id": w.a.member_id},
        f"{p}/branches/{{branch_id}}/comments": {"body": "probe"},
        f"{p}/branches/{{branch_id}}/revert": {
            "entity_type": "event",
            "name": f"{MARKER}_purchase",
        },
        f"{p}/branches/{{branch_id}}/resolutions": {
            "entity_type": "event",
            "entity_name": f"{MARKER}_purchase",
            "field_name": "description",
            "choice": "ours",
        },
        f"{p}/subscriptions/{{entity_type}}/{{entity_id}}": (
            {"muted": True} if method == "PATCH" else {}
        ),
        f"{API}/orgs/{{org}}": {"name": "probe"} if method == "PATCH" else {"confirm_slug": ORG_A},
        f"{API}/orgs/{{org}}/members/{{user_id}}": {"role": "member"},
        f"{API}/orgs/{{org}}/transfer-ownership": {"user_id": w.a.member_id},
        f"{API}/orgs/{{org}}/groups": {"name": "probe"},
        f"{API}/orgs/{{org}}/groups/{{group_id}}": {"name": "probe"},
        f"{API}/orgs/{{org}}/groups/{{group_id}}/members": {"user_id": w.a.member_id},
        f"{API}/audit/webhook": {"url": "https://hooks.example.com/probe", "enabled": True},
    }
    return table.get(path, {})


def _to_org(url: str, org: str) -> str:
    return url.replace(f"{API}/", f"{API}/orgs/{org}/", 1)


def _leaks(
    resp: Response, markers: list[str], *, echoed: dict[str, str] | None = None
) -> list[str]:
    """Markers in the body, beyond the query values the route echoes back.

    ``docs/links`` and ``dependencies`` answer with the ref they were asked
    about (``kind:name`` echoed whole and split, marked broken or not
    existing); that echo is the caller's own input, not a read of A. Anything A
    really holds (a doc path, a second name, another id) still shows up.
    """
    text = resp.content.decode("utf-8", "replace")
    for value in (echoed or {}).values():
        for part in sorted({value, *value.split(":")}, key=len, reverse=True):
            text = text.replace(part, "")
    text = text.lower()
    return [m for m in markers if m.lower() in text]


def _normalized(resp: Response, ids: list[str]) -> str:
    text = resp.content.decode("utf-8", "replace")
    for value in ids:
        text = text.replace(value, "<id>")
    return text


# ── the coverage guard ───────────────────────────────────────────────────────


def test_every_route_is_matrixed_or_allowlisted() -> None:
    routes = iter_api_routes()
    paths = {path for path, _ in routes}

    stale = sorted(set(PUBLIC_OR_INSTANCE_WIDE) - paths)
    assert stale == [], f"allowlisted routes that no longer exist: {stale}"
    stale_legacy = sorted(k for k in LEGACY_NOT_DRIVEN if k[1] not in paths)
    assert stale_legacy == [], f"LEGACY_NOT_DRIVEN names missing routes: {stale_legacy}"

    unreachable: list[str] = []
    unknown_params: list[str] = []
    for method, path in _matrix_routes():
        # A per-organization route must have an org-qualified form; otherwise it
        # belongs in the allowlist, with a reason.
        if not (_org_rewritable(path) or _org_addressed(path)):
            unreachable.append(f"{method} {path}")
        unknown_params.extend(
            f"{{{name}}} in {method} {path}"
            for name in _PARAM.findall(path)
            if name not in KNOWN_PARAMS
        )
    assert unreachable == [], (
        "routes outside ORG_REWRITE_PREFIXES and /orgs/{org} that are not allowlisted in "
        f"PUBLIC_OR_INSTANCE_WIDE: {unreachable}"
    )
    assert unknown_params == [], (
        f"new path parameters: teach _a_value which A row they name: {unknown_params}"
    )
    # The walker must really see the app, not one router (see iter_api_routes).
    assert len(_matrix_routes()) > 250
    assert all(isinstance(route, APIRoute) for _, route in routes)


# ── the org-qualified path: A's organization does not exist for B ───────────


@pytest.mark.parametrize("actor_name", ["b-owner", "b-member", "b-api-key"])
async def test_org_qualified_routes_of_org_a_answer_404_to_org_b(
    world: World, actor_name: str
) -> None:
    actor = next(a for a in world.b_actors(with_bridge=False) if a.name == actor_name)
    failures: list[str] = []
    for method, path in _matrix_routes():
        a_url = _a_url(path, world)
        url = a_url if _org_addressed(path) else _to_org(a_url, ORG_A)
        resp = await actor.call(
            method, url, params=_query(path, world), body=_body(method, path, world)
        )
        if resp.status_code != 404:
            failures.append(f"{method} {url} -> {resp.status_code} {resp.text[:160]}")
    assert failures == [], "\n".join(failures)

    # Not vacuous: A's own owner is let through the same prefix.
    own = await world.a.owner.get(f"{API}/orgs/{ORG_A}/projects/{SLUG}/events")
    assert own.status_code == 200, own.text
    assert MARKER in own.text
    members = await world.a.owner.get(f"{API}/orgs/{ORG_A}/members")
    assert members.status_code == 200, members.text
    assert world.a.member_id in members.text


# ── the legacy path: acting in B, A's ids answer like unknown ids ───────────


async def _snapshot(w: World) -> dict[str, str]:
    """What A's owner reads of A: must not move while B probes it."""
    base = f"{API}/projects/{SLUG}"
    reads = [
        f"{API}/projects",
        f"{API}/data-sources",
        f"{API}/users",
        f"{API}/users/invitations",
        f"{API}/me/api-keys",
        base,
        f"{base}/members",
        f"{base}/event-types",
        f"{base}/events",
        f"{base}/relations",
        f"{base}/meta-fields",
        f"{base}/variables",
        f"{base}/annotations",
        f"{base}/planned-events",
        f"{base}/branches",
        f"{base}/revisions",
        f"{base}/docs",
        f"{base}/alert-destinations",
        f"{base}/scans",
        f"{base}/metrics",
        f"{base}/fact-tables",
        f"{base}/event-type-owners",
        f"{base}/events/{w.a.ids['event_id']}/comments",
        f"{base}/branches/{w.a.ids['branch_id']}/comments",
        f"{base}/alert-inbox",
    ]
    out: dict[str, str] = {}
    for url in reads:
        resp = await w.a.owner.get(url)
        assert resp.status_code == 200, f"{url}: {resp.text}"
        out[url] = json.dumps(resp.json(), sort_keys=True)
    return out


@pytest.mark.parametrize("actor_name", ["b-owner", "b-member", "b-api-key", "bridge"])
async def test_legacy_routes_in_org_b_never_reach_org_a(world: World, actor_name: str) -> None:
    actor = next(a for a in world.b_actors(with_bridge=True) if a.name == actor_name)
    markers = world.a_markers()
    a_ids = [v for k, v in world.a.ids.items() if k != "entity_type"]
    before = await _snapshot(world)

    failures: list[str] = []
    for method, path in _matrix_routes():
        if (method, path) in LEGACY_NOT_DRIVEN or _org_addressed(path):
            # ``/orgs/{org}/...`` has no legacy form: the org-qualified matrix
            # above drives it against A.
            continue
        names = [n for n in _PARAM.findall(path) if n != "slug"]
        own = actor.own_prefix
        if not names:
            if method != "GET":
                # A write that names no id acts on B's own rows; its org-qualified
                # form is driven above and bodies carrying A's ids below.
                continue
            # ``{slug}`` is B's own ``web``; left unfilled, every slug route 404ed
            # on the literal ``{slug}`` and never read anything.
            url = _fill(path, lambda _name: SLUG).replace(API, own, 1)
            query = _query(path, world)
            resp = await actor.call(method, url, params=query)
            if 200 <= resp.status_code < 300:
                if leaked := _leaks(resp, markers, echoed=query):
                    failures.append(f"{method} {url} -> {resp.status_code} leaks {leaked}")
            elif (
                actor.name == "b-owner" and not path.endswith(QUERY_NAMES_AN_A_ROW)
            ) or resp.status_code >= 500:
                # B's owner may read every id-less route of B: anything else
                # means the route never reached B's rows, so it proved nothing.
                failures.append(f"{method} {url} -> {resp.status_code} {resp.text[:160]}")
            continue

        a_url = _a_url(path, world).replace(API, own, 1)
        control_url = _control_url(path).replace(API, own, 1)
        params = _query(path, world)
        body = _body(method, path, world)
        resp = await actor.call(method, a_url, params=params, body=body)
        control = await actor.call(method, control_url, params=params, body=body)
        if resp.status_code >= 500 or control.status_code >= 500:
            # Two identical 500s would compare equal below, and a lookup that
            # ignores the organization fails exactly that way on the shared
            # ``web`` (MultipleResultsFound).
            failures.append(
                f"{method} {a_url} -> {resp.status_code}, unknown id -> "
                f"{control.status_code} {control.text[:120]}"
            )
            continue
        if 200 <= resp.status_code < 300:
            failures.append(f"{method} {a_url} -> {resp.status_code} {resp.text[:160]}")
            continue
        control_ids = re.findall(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", control_url
        )
        if resp.status_code != control.status_code or _normalized(resp, a_ids) != _normalized(
            control, control_ids
        ):
            failures.append(
                f"{method} {a_url} -> {resp.status_code} {resp.text[:120]} but an unknown id "
                f"-> {control.status_code} {control.text[:120]}"
            )
        elif leaked := _leaks(resp, [MARKER]):
            failures.append(f"{method} {a_url} -> {resp.status_code} leaks {leaked}")

    failures.extend(await _a_only_slug_failures(actor))
    assert failures == [], "\n".join(failures)
    assert await _snapshot(world) == before


async def _a_only_slug_failures(actor: Actor) -> list[str]:
    """A slug only A holds answers B exactly like a slug nobody holds."""
    unknown = f"zz-{uuid.uuid4().hex[:8]}"
    failures: list[str] = []
    for template in ("/projects/{slug}", "/activity/projects/{slug}"):
        a_url = f"{actor.own_prefix}{template.format(slug=A_ONLY_SLUG)}"
        control_url = f"{actor.own_prefix}{template.format(slug=unknown)}"
        resp = await actor.call("GET", a_url)
        control = await actor.call("GET", control_url)
        body = resp.content.decode("utf-8", "replace").replace(A_ONLY_SLUG, "<slug>")
        control_body = control.content.decode("utf-8", "replace").replace(unknown, "<slug>")
        if resp.status_code != 404 or control.status_code != 404 or body != control_body:
            failures.append(
                f"GET {a_url} -> {resp.status_code} {resp.text[:120]} but an unknown slug "
                f"-> {control.status_code} {control.text[:120]}"
            )
        elif MARKER in body.lower():
            failures.append(f"GET {a_url} leaks {MARKER}")
    return failures


# ── B's lists ────────────────────────────────────────────────────────────────

LIST_READS: list[tuple[str, dict[str, str] | None]] = [
    ("/projects", None),
    ("/data-sources", None),
    ("/users", None),
    ("/users/invitations", None),
    ("/audit", None),
    ("/audit/actions", None),
    # The export (owner/admin) and the webhook's deliveries (owner): B's rows only.
    ("/audit/export", None),
    ("/audit/export", {"format": "json"}),
    ("/audit/webhook", None),
    ("/audit/webhook/deliveries", None),
    ("/activity", None),
    (f"/activity/projects/{SLUG}", None),
    ("/me/api-keys", None),
    ("/me/notifications", None),
    ("/me/notifications/unread-count", None),
    (f"/projects/{SLUG}", None),
    (f"/projects/{SLUG}/search", {"q": MARKER}),
    (f"/projects/{SLUG}/search", {"q": "purchase"}),
    (f"/projects/{SLUG}/docs", None),
    (f"/projects/{SLUG}/docs/search", {"q": MARKER}),
    # The note editor's picker: notes, people and entities of B's project only.
    (f"/projects/{SLUG}/docs/link-suggestions", {"q": MARKER}),
    (f"/projects/{SLUG}/docs/link-suggestions", {"limit": "50"}),
    (f"/projects/{SLUG}/docs/export", {"scope": "organization"}),
    (f"/projects/{SLUG}/event-types", None),
    (f"/projects/{SLUG}/events", None),
    (f"/projects/{SLUG}/members", None),
    (f"/projects/{SLUG}/alert-inbox", None),
    (f"/projects/{SLUG}/alert-deliveries", None),
    (f"/projects/{SLUG}/revisions", None),
    (f"/projects/{SLUG}/plan/export", None),
]


async def test_org_b_lists_carry_nothing_of_org_a(world: World) -> None:
    markers = world.a_markers()
    failures: list[str] = []
    for actor in world.b_actors(with_bridge=True):
        for suffix, params in LIST_READS:
            url = f"{actor.own_prefix}{suffix}"
            resp = await actor.call("GET", url, params=params)
            if resp.status_code == 403 and actor.name != "b-owner":
                # Owner-only in B (audit, invitations) or session-only (keys
                # may not list keys): B's owner session reads every one.
                continue
            if resp.status_code != 200:
                failures.append(f"{actor.name} GET {url} -> {resp.status_code} {resp.text[:120]}")
            elif leaked := _leaks(resp, markers):
                failures.append(f"{actor.name} GET {url} leaks {leaked}")
        # Organization groups have no legacy form: B's own list, under B's path.
        groups_url = f"{API}/orgs/{ORG_B}/groups"
        groups = await actor.call("GET", groups_url)
        if groups.status_code != 200:
            failures.append(f"{actor.name} GET {groups_url} -> {groups.status_code}")
        elif leaked := _leaks(groups, markers):
            failures.append(f"{actor.name} GET {groups_url} leaks {leaked}")
        elif [g["id"] for g in groups.json()] != [world.b.ids["group_id"]]:
            failures.append(f"{actor.name} GET {groups_url} -> {groups.text[:120]}")

    # B's lists are not empty by accident: B sees its own ``web`` and source.
    projects = (await world.b.owner.get(f"{API}/projects")).json()
    assert [p["id"] for p in _items(projects)] == [world.b.ids["project_id"]]
    sources = (await world.b.owner.get(f"{API}/data-sources")).json()
    assert [s["id"] for s in _items(sources)] == [world.b.ids["ds_id"]]
    assert failures == [], "\n".join(failures)


def _items(payload: Any) -> list[dict[str, Any]]:
    return payload["items"] if isinstance(payload, dict) else payload


async def test_the_bridge_reads_each_bell_under_its_own_organization(world: World) -> None:
    in_b = await world.bridge.get(f"{API}/orgs/{ORG_B}/me/notifications")
    in_a = await world.bridge.get(f"{API}/orgs/{ORG_A}/me/notifications")
    assert in_b.status_code == in_a.status_code == 200
    assert world.notification_id not in in_b.text
    assert world.notification_id in in_a.text
    count_b = await world.bridge.get(f"{API}/orgs/{ORG_B}/me/notifications/unread-count")
    assert count_b.json()["unread"] == 0

    # Marking A's notification read from B's bell does nothing to it.
    marked = await world.bridge.post(
        f"{API}/orgs/{ORG_B}/me/notifications/read", json={"ids": [world.notification_id]}
    )
    assert marked.status_code == 200, marked.text
    count_a = await world.bridge.get(f"{API}/orgs/{ORG_A}/me/notifications/unread-count")
    assert count_a.json()["unread"] == 1
    # The legacy path is ambiguous for someone in two hosted organizations.
    assert (await world.bridge.get(f"{API}/me/notifications")).status_code == 400


# ── A's ids smuggled in a body of B's own project ───────────────────────────


def _body_probes(w: World) -> list[tuple[str, str, dict[str, Any], str]]:
    """``(method, suffix, body, kind)``: kind ``refuse`` must be non-2xx,
    ``ignore`` may succeed but must leave A unchanged (checked by the snapshot)."""
    a = w.a.ids
    b = w.b.ids
    base = f"/projects/{SLUG}"
    return [
        ("POST", f"{base}/events", {"event_type_id": a["event_type_id"], "name": "x_ev"}, "refuse"),
        (
            "POST",
            f"{base}/relations",
            {
                "source_event_type_id": a["event_type_id"],
                "target_event_type_id": b["event_type_id"],
                "source_field_id": a["field_id"],
                "target_field_id": b["field_id"],
            },
            "refuse",
        ),
        (
            "POST",
            f"{base}/scans",
            {"data_source_id": a["ds_id"], "name": "x_scan", "base_query": "SELECT 1"},
            "refuse",
        ),
        (
            "POST",
            f"{base}/metrics",
            {
                "kind": "sql",
                "name": "x_metric",
                "display_name": "X",
                "data_source_id": a["ds_id"],
                "interval": "1d",
                "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
            },
            "refuse",
        ),
        (
            "POST",
            f"{base}/fact-tables",
            {
                "name": "x_orders",
                "display_name": "X",
                "sql": "SELECT created_at FROM orders",
                "timestamp_column": "created_at",
                "data_source_id": a["ds_id"],
                "columns": [{"name": "created_at", "type": "timestamp"}],
            },
            "refuse",
        ),
        ("POST", f"{base}/members", {"user_id": w.a.member_id, "role": "viewer"}, "refuse"),
        (
            "POST",
            f"{base}/event-types/{b['event_type_id']}/owners",
            {"user_id": w.a.member_id},
            "refuse",
        ),
        (
            "POST",
            f"{base}/branches/{b['branch_id']}/reviewers",
            {"user_id": w.a.member_id},
            "refuse",
        ),
        ("POST", f"{base}/events/bulk-delete", {"event_ids": [a["event_id"]]}, "ignore"),
        (
            "POST",
            f"{base}/events/bulk-update",
            {"event_ids": [a["event_id"]], "status": "deprecated"},
            "ignore",
        ),
        ("PATCH", f"{base}/events/reorder", {"event_ids": [a["event_id"]]}, "ignore"),
        ("POST", f"{base}/variables/bulk-delete", {"variable_ids": [a["variable_id"]]}, "ignore"),
        (
            "POST",
            f"{base}/variables/bulk-update",
            {"variable_ids": [a["variable_id"]], "description": "x"},
            "ignore",
        ),
        (
            "POST",
            f"{base}/metrics/bulk-update",
            {"metric_ids": [a["metric_id"]], "reviewed": True},
            "ignore",
        ),
        (
            "POST",
            f"{base}/duplicates/dismiss",
            {"event_a_id": a["event_id"], "event_b_id": b["event_id"]},
            "refuse",
        ),
        ("POST", "/me/notifications/read", {"ids": [w.notification_id]}, "ignore"),
    ]


async def test_org_a_ids_in_bodies_of_org_b_are_refused_or_ignored(world: World) -> None:
    before = await _snapshot(world)
    markers = world.a_markers()
    failures: list[str] = []
    for method, suffix, body, kind in _body_probes(world):
        resp = await world.b.owner.request(method, f"{API}{suffix}", json=body)
        ok = 200 <= resp.status_code < 300
        if kind == "refuse" and ok:
            failures.append(f"{method} {suffix} accepted A's id -> {resp.status_code} {resp.text}")
        elif ok and (leaked := _leaks(resp, markers)):
            failures.append(f"{method} {suffix} leaks {leaked}")
    assert failures == [], "\n".join(failures)
    assert await _snapshot(world) == before
    unread = await world.bridge.get(f"{API}/orgs/{ORG_A}/me/notifications/unread-count")
    assert unread.json()["unread"] == 1


# ── demos: the seeded trail and the cancel stay in their organization ───────


async def test_a_demo_seeded_in_org_a_files_its_audit_trail_in_org_a(world: World) -> None:
    """Every ``demo_seed`` row is A's; neither B's nor the default org's feed shows it."""
    created = await world.a.owner.post(f"{API}/orgs/{ORG_A}/projects/demo")
    assert created.status_code == 202, created.text
    demo_slug = created.json()["slug"]

    async with TestSessionLocal() as session:
        entries = (await session.execute(select(AuditLog))).scalars().all()
    seeded = [entry for entry in entries if (entry.payload or {}).get("demo_seed")]
    assert seeded, "the demo recipe filed no audit rows"
    assert {entry.organization_id for entry in seeded} == {ORG_A_ID}
    # Instance-scoped rows (no project) are the demo organization's too.
    assert any(entry.project_id is None for entry in seeded)

    own = await world.a.owner.get(
        f"{API}/orgs/{ORG_A}/audit", params={"project_slug": demo_slug, "limit": "200"}
    )
    assert own.status_code == 200, own.text
    assert own.json()["items"], "A's own audit tab shows nothing of its demo"

    default_owner = _new_client()
    try:
        default_owner_id = await _register(default_owner, "default-owner@example.com", "Default")
        async with TestSessionLocal() as session:
            await add_org_member(session, uuid.UUID(default_owner_id), "owner")
        for name, client, url in (
            ("b-owner", world.b.owner, f"{API}/audit"),
            ("default-owner", default_owner, f"{API}/audit"),
        ):
            feed = await client.get(url, params={"limit": "200"})
            assert feed.status_code == 200, f"{name}: {feed.text}"
            assert demo_slug not in feed.text, f"{name} reads A's demo trail"
            scoped = await client.get(url, params={"project_slug": demo_slug})
            assert scoped.status_code == 200, f"{name}: {scoped.text}"
            assert scoped.json()["items"] == [], f"{name} reads A's demo trail by slug"
    finally:
        await default_owner.aclose()


async def test_cancelling_a_demo_in_org_b_leaves_org_a_demos_alone(world: World) -> None:
    """``POST /projects/demo/cancel`` acts only in the organization it is bound to."""
    async with TestSessionLocal() as session:
        shell = Project(
            name="Demo Project",
            slug=f"demo-{MARKER}",
            is_demo=True,
            generation_status=ProjectGenerationStatus.seeding.value,
            generation_stage="init",
            created_by_user_id=uuid.UUID(world.bridge_id),
            organization_id=ORG_A_ID,
        )
        session.add(shell)
        await session.commit()
        shell_id = shell.id

    async def cancel(org: str) -> dict[str, Any]:
        resp = await world.bridge.post(f"{API}/orgs/{org}/projects/demo/cancel")
        assert resp.status_code == 200, resp.text
        return dict(resp.json())

    async def stage() -> str | None:
        async with TestSessionLocal() as session:
            return await session.scalar(
                select(Project.generation_stage).where(Project.id == shell_id)
            )

    # Seeding in A: B's cancel neither flags it nor names it.
    assert await cancel(ORG_B) == {"cancelled": False, "slug": None, "state": "none"}
    assert await stage() == "init"

    # Just finished in A: B's cancel does not hand A's slug back as B's.
    async with TestSessionLocal() as session:
        ready = await session.get(Project, shell_id)
        assert ready is not None
        ready.generation_status = ProjectGenerationStatus.ready.value
        ready.generation_stage = None
        await session.commit()
    assert await cancel(ORG_B) == {"cancelled": False, "slug": None, "state": "none"}
    # Not vacuous: under A the same call reports A's demo.
    in_a = await cancel(ORG_A)
    assert (in_a["state"], in_a["slug"]) == ("finished", f"demo-{MARKER}")
