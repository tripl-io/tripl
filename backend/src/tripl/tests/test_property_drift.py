"""Property drift (F23.5) and the per-event required threshold (F23.4.5)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tripl.core.property_drift import (
    DEFAULT_REQUIRED_PRESENCE,
    event_findings,
    type_admits,
    type_findings,
)
from tripl.models.property_drift import PropertyDrift, PropertyDriftKind
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue
from tripl.tests.conftest import TestSessionLocal

V1, V2, V3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
E1, E2 = uuid.uuid4(), uuid.uuid4()


def test_a_new_key_is_reported_only_against_a_described_event() -> None:
    findings = event_findings(
        presence={(V1, E1): 1.0, (V2, E1): 0.3, (V2, E2): 0.5},
        measured_events={E1, E2},
        entries={E1: {V1: False}},
        thresholds={},
        json_variables=set(),
    )
    assert [(f.variable_id, f.event_id, f.kind) for f in findings] == [
        (V2, E1, PropertyDriftKind.new_property)
    ]
    assert findings[0].detail == {"presence_rate": 0.3}


def test_a_required_key_below_the_threshold_or_absent_is_missing() -> None:
    findings = event_findings(
        presence={(V1, E1): 0.97, (V2, E1): 0.5},
        measured_events={E1},
        entries={E1: {V1: True, V2: True, V3: True}},
        thresholds={E1: None},
        json_variables={V1, V2, V3},
    )
    missing = {f.variable_id: f.detail for f in findings}
    assert missing == {
        V2: {"presence_rate": 0.5, "threshold": DEFAULT_REQUIRED_PRESENCE},
        V3: {"presence_rate": 0.0, "threshold": DEFAULT_REQUIRED_PRESENCE},
    }


def test_the_threshold_is_the_events_own() -> None:
    findings = event_findings(
        presence={(V1, E1): 0.5},
        measured_events={E1},
        entries={E1: {V1: True}},
        thresholds={E1: 0.4},
        json_variables={V1},
    )
    assert findings == []


def test_a_required_property_no_json_column_carries_is_not_judged() -> None:
    findings = event_findings(
        presence={},
        measured_events={E1},
        entries={E1: {V1: True}},
        thresholds={},
        json_variables=set(),
    )
    assert findings == []


@pytest.mark.parametrize(
    ("expected", "observed", "admitted"),
    [
        ("string", "string", True),
        ("string", "datetime", True),
        ("string", "number", False),
        ("datetime", "date", True),
        ("date", "datetime", False),
        ("json", "string_array", True),
        ("number", "string", False),
    ],
)
def test_type_admits(expected: str, observed: str, admitted: bool) -> None:
    assert type_admits(expected, observed) is admitted


def test_type_change_skips_placeholder_scan_variables() -> None:
    from tripl.core.analyzers._event_generator_variables import SCAN_PROVENANCE_DESCRIPTION

    placeholder = Variable(
        id=V1,
        name="a",
        source_name="payload.a",
        variable_type="string",
        description=SCAN_PROVENANCE_DESCRIPTION,
        bindings=["payload.a"],
        excluded_from_scans=False,
    )
    typed = Variable(
        id=V2,
        name="b",
        source_name="payload.b",
        variable_type="number",
        description=SCAN_PROVENANCE_DESCRIPTION,
        bindings=["payload.b"],
        excluded_from_scans=False,
    )
    findings = type_findings(
        [placeholder, typed],
        {"payload.a": ("number", None), "payload.b": ("string", None)},
    )
    assert [(f.variable_id, f.detail["observed_type"]) for f in findings] == [(V2, "string")]


# --- API ---------------------------------------------------------------------

EVENT = "purchase:success"


async def _seed(client: AsyncClient, slug: str) -> tuple[str, str, str]:
    await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et.json()['id']}/fields",
        json={"name": "payload", "display_name": "Payload", "field_type": "json"},
    )
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et.json()["id"], "name": EVENT},
    )
    variable = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "amount", "variable_type": "number", "bindings": ["payload.amount"]},
    )
    assert variable.status_code == 201, variable.text
    return variable.json()["id"], event.json()["id"], field.json()["id"]


async def _drift(var_id: str, event_id: str | None, kind: str, detail: dict) -> str:
    async with TestSessionLocal() as session, session.begin():
        variable = await session.get(Variable, uuid.UUID(var_id))
        assert variable is not None
        drift = PropertyDrift(
            project_id=variable.project_id,
            variable_id=variable.id,
            event_id=uuid.UUID(event_id) if event_id else None,
            kind=kind,
            detail=detail,
        )
        session.add(drift)
    return str(drift.id)


async def _act(client: AsyncClient, slug: str, drift_id: str, body: dict):
    return await client.post(
        f"/api/v1/projects/{slug}/variables/property-drifts/{drift_id}/action", json=body
    )


@pytest.mark.asyncio
async def test_accepting_a_new_property_puts_it_on_the_list(client: AsyncClient) -> None:
    slug = "pdrift-new"
    var_id, event_id, _ = await _seed(client, slug)
    drift_id = await _drift(var_id, event_id, "new_property", {"presence_rate": 0.3})

    listed = await client.get(f"/api/v1/projects/{slug}/variables/property-drifts?active_only=true")
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["items"]
    assert (item["kind"], item["variable_name"], item["event_name"]) == (
        "new_property",
        "amount",
        EVENT,
    )

    resp = await _act(client, slug, drift_id, {"action": "accept"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "accepted"
    props = (await client.get(f"/api/v1/projects/{slug}/events/{event_id}/properties")).json()
    assert [(p["name"], p["required"], p["values"]) for p in props] == [("amount", False, None)]
    after = await client.get(f"/api/v1/projects/{slug}/variables/property-drifts?active_only=true")
    assert after.json()["total"] == 0


@pytest.mark.asyncio
async def test_accepting_a_missing_required_property_makes_it_optional(
    client: AsyncClient,
) -> None:
    slug = "pdrift-missing"
    var_id, event_id, _ = await _seed(client, slug)
    await client.put(
        f"/api/v1/projects/{slug}/variables/{var_id}/event-overrides/{event_id}",
        json={"required": True},
    )
    drift_id = await _drift(
        var_id, event_id, "missing_required", {"presence_rate": 0.5, "threshold": 0.95}
    )
    resp = await _act(client, slug, drift_id, {"action": "accept", "note": "optional by design"})
    assert resp.status_code == 200, resp.text
    [prop] = (await client.get(f"/api/v1/projects/{slug}/events/{event_id}/properties")).json()
    assert prop["required"] is False


@pytest.mark.asyncio
async def test_accepting_a_type_change_retypes_the_variable(client: AsyncClient) -> None:
    slug = "pdrift-type"
    var_id, _, _ = await _seed(client, slug)
    drift_id = await _drift(
        var_id,
        None,
        "type_change",
        {"expected_type": "number", "observed_type": "string", "observed_schema": None},
    )
    resp = await _act(client, slug, drift_id, {"action": "accept"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["event_id"] is None
    items = (await client.get(f"/api/v1/projects/{slug}/variables")).json()["items"]
    assert items[0]["variable_type"] == "string"


@pytest.mark.asyncio
async def test_snooze_needs_a_future_instant(client: AsyncClient) -> None:
    slug = "pdrift-snooze"
    var_id, event_id, _ = await _seed(client, slug)
    drift_id = await _drift(var_id, event_id, "new_property", {"presence_rate": 0.3})
    assert (await _act(client, slug, drift_id, {"action": "snooze"})).status_code == 422
    until = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    snoozed = await _act(client, slug, drift_id, {"action": "snooze", "snoozed_until": until})
    assert snoozed.json()["status"] == "snoozed"
    reopened = await _act(client, slug, drift_id, {"action": "reopen"})
    assert reopened.json()["status"] == "open"


@pytest.mark.asyncio
async def test_suggested_required_follows_the_events_threshold(client: AsyncClient) -> None:
    slug = "pdrift-suggest"
    var_id, event_id, field_id = await _seed(client, slug)
    await client.put(
        f"/api/v1/projects/{slug}/variables/{var_id}/event-overrides/{event_id}", json={}
    )
    [prop] = (await client.get(f"/api/v1/projects/{slug}/events/{event_id}/properties")).json()
    assert prop["suggested_required"] is None

    async with TestSessionLocal() as session, session.begin():
        variable = await session.get(Variable, uuid.UUID(var_id))
        assert variable is not None
        session.add(
            VariableValue(
                project_id=variable.project_id,
                branch_id=variable.branch_id,
                variable_id=variable.id,
                event_id=uuid.UUID(event_id),
                field_definition_id=uuid.UUID(field_id),
                source_column="payload.amount",
                value_kind="high",
                observed_count=1,
                values=["10"],
                presence_rate=0.9,
            )
        )
    [prop] = (await client.get(f"/api/v1/projects/{slug}/events/{event_id}/properties")).json()
    assert prop["suggested_required"] is False

    patched = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}", json={"required_presence_threshold": 0.85}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["required_presence_threshold"] == 0.85
    [prop] = (await client.get(f"/api/v1/projects/{slug}/events/{event_id}/properties")).json()
    assert prop["suggested_required"] is True

    refused = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}", json={"required_presence_threshold": 1.5}
    )
    assert refused.status_code == 422


@pytest.mark.asyncio
async def test_the_threshold_travels_with_a_branch(client: AsyncClient) -> None:
    slug = "pdrift-branch"
    _, event_id, _ = await _seed(client, slug)
    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    branch_id = branch.json()["id"]
    events = (await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")).json()
    b_event = events["items"][0]["id"]
    await client.patch(
        f"/api/v1/projects/{slug}/events/{b_event}?branch={branch_id}",
        json={"required_presence_threshold": 0.7},
    )
    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    assert "required_presence_threshold" in str(diff)
    for action in ("submit", "approve"):
        await client.post(
            f"/api/v1/projects/{slug}/branches/{branch_id}/transition", json={"action": action}
        )
    merged = await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
    assert merged.status_code == 200, merged.text
    on_main = (await client.get(f"/api/v1/projects/{slug}/events/{event_id}")).json()
    assert on_main["required_presence_threshold"] == 0.7


def test_an_accepted_finding_reopens_when_it_recurs() -> None:
    """Accepting changes the plan so the finding cannot recur; if it does, it is new."""
    from tripl.core.property_drift import Finding, upsert_findings
    from tripl.models.schema_drift import SCHEMA_DRIFT_STATUS_ACCEPTED

    class _Session:
        def __init__(self, rows: list[PropertyDrift]) -> None:
            self.rows = rows

        def execute(self, _query: object):
            rows = self.rows

            class _Result:
                def scalars(self):
                    return iter(rows)

            return _Result()

        def add(self, row: PropertyDrift) -> None:
            self.rows.append(row)

    project_id = uuid.uuid4()
    accepted = PropertyDrift(
        project_id=project_id,
        variable_id=V1,
        event_id=E1,
        kind="new_property",
        status=SCHEMA_DRIFT_STATUS_ACCEPTED,
        detail={},
    )
    session = _Session([accepted])
    upsert_findings(
        session,  # type: ignore[arg-type]
        project_id=project_id,
        scan_config_id=None,
        findings=[Finding(V1, E1, PropertyDriftKind.new_property, {"presence_rate": 0.2})],
    )
    assert accepted.status == "open"
    assert accepted.detail == {"presence_rate": 0.2}
