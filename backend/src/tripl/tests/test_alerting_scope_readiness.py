"""An enabled drift scope that nothing can feed says so.

Production shape this pins: every acme-ios scan config carried
``distribution_drift_fields=[]`` and 0 of 1793 variables documented
``allowed_values``, while the only monitor had both drift scopes switched on.
Both scopes were structurally unable to fire and no response said so.

The interesting cases are the ones that must NOT warn — a drift row collected
against a config whose field list is now empty, an open value drift left behind
by a documented list the operator then emptied, and a per-event override with no
global list — because a warning painted over a healthy project is worse than the
silence it replaced.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import DistributionDriftBand
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.schema_drift import SCHEMA_DRIFT_STATUS_ACCEPTED, SCHEMA_DRIFT_STATUS_OPEN
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.tests.conftest import TestSessionLocal


async def _make_project(client: AsyncClient, slug: str) -> uuid.UUID:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": slug, "slug": slug, "description": ""},
    )
    assert resp.status_code == 201
    return uuid.UUID(resp.json()["id"])


async def _make_scan(
    client: AsyncClient,
    slug: str,
    *,
    distribution_drift_fields: list[str],
) -> uuid.UUID:
    data_source_resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": f"Warehouse {slug}",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert data_source_resp.status_code == 201
    scan_resp = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={
            "data_source_id": data_source_resp.json()["id"],
            "name": "Production scan",
            "base_query": "SELECT 1",
            "distribution_drift_fields": distribution_drift_fields,
        },
    )
    assert scan_resp.status_code == 201
    return uuid.UUID(scan_resp.json()["id"])


async def _main_branch_id(project_id: uuid.UUID) -> uuid.UUID:
    """The branch detection reads. Creating the project already made it."""
    async with TestSessionLocal() as session:
        return (
            await session.execute(
                select(PlanBranch.id).where(
                    PlanBranch.project_id == project_id,
                    PlanBranch.kind == BranchKind.main.value,
                )
            )
        ).scalar_one()


async def _add(*rows: object) -> None:
    async with TestSessionLocal() as session:
        session.add_all(list(rows))
        await session.commit()


def _variable(
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    *,
    name: str,
    allowed_values: list[str],
    excluded_from_scans: bool = False,
) -> Variable:
    return Variable(
        id=uuid.uuid4(),
        project_id=project_id,
        branch_id=branch_id,
        name=name,
        allowed_values=allowed_values,
        excluded_from_scans=excluded_from_scans,
    )


async def _make_event(client: AsyncClient, slug: str) -> uuid.UUID:
    """An event to anchor a per-event override or a value drift row to."""
    event_type_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    assert event_type_resp.status_code == 201
    event_resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type_resp.json()["id"],
            "name": "Landing Viewed",
            "status": "implemented",
        },
    )
    assert event_resp.status_code == 201
    return uuid.UUID(event_resp.json()["id"])


def _value_drift_row(
    project_id: uuid.UUID,
    variable_id: uuid.UUID,
    event_id: uuid.UUID,
    *,
    scan_config_id: uuid.UUID | None,
    status: str = SCHEMA_DRIFT_STATUS_OPEN,
    detected_at: datetime | None = None,
) -> VariableValueDrift:
    return VariableValueDrift(
        id=uuid.uuid4(),
        project_id=project_id,
        variable_id=variable_id,
        event_id=event_id,
        scan_config_id=scan_config_id,
        observed_values=["holdout"],
        status=status,
        detected_at=detected_at or datetime.now(UTC),
    )


def _drift_row(scan_config_id: uuid.UUID) -> DistributionDrift:
    return DistributionDrift(
        id=uuid.uuid4(),
        scan_config_id=scan_config_id,
        event_type_id=None,
        field_name="platform",
        bucket=datetime(2026, 8, 20, tzinfo=UTC),
        psi=0.42,
        band=DistributionDriftBand.significant.value,
        baseline_total=1000,
        current_total=900,
        top_movers=[],
    )


async def _readiness(client: AsyncClient, slug: str) -> dict[str, bool]:
    resp = await client.get(f"/api/v1/projects/{slug}/monitors-summary")
    assert resp.status_code == 200
    return resp.json()["scope_readiness"]


@pytest.mark.asyncio
async def test_scope_readiness_false_when_nothing_documents_values(client: AsyncClient) -> None:
    # Arrange — the production shape: variables exist but none documents a list,
    # a scan exists but watches no column, and nothing has been collected.
    project_id = await _make_project(client, "readiness-inert")
    branch_id = await _main_branch_id(project_id)
    await _make_scan(client, "readiness-inert", distribution_drift_fields=[])
    await _add(
        _variable(project_id, branch_id, name="variant", allowed_values=[]),
        _variable(project_id, branch_id, name="plan", allowed_values=[]),
    )

    # Act
    readiness = await _readiness(client, "readiness-inert")

    # Assert
    assert readiness == {"variable_value_drift": False, "distribution_drift": False}


@pytest.mark.asyncio
async def test_scope_readiness_true_when_a_variable_documents_allowed_values(
    client: AsyncClient,
) -> None:
    # Arrange
    project_id = await _make_project(client, "readiness-documented")
    branch_id = await _main_branch_id(project_id)
    await _add(_variable(project_id, branch_id, name="variant", allowed_values=["a", "b"]))

    # Act
    readiness = await _readiness(client, "readiness-documented")

    # Assert — one documented variable is a contract the scope can drift against.
    assert readiness["variable_value_drift"] is True
    assert readiness["distribution_drift"] is False


@pytest.mark.asyncio
async def test_scope_readiness_true_from_a_per_event_override(client: AsyncClient) -> None:
    """A per-event override REPLACES the global list, so it alone is enough.

    Pins the override branch of ``_variable_value_drift``: the detector reads
    ``overrides.get(pair, variable.allowed_values)``, so a variable with an
    empty global list still drifts on any pair an override documents.
    """
    # Arrange
    project_id = await _make_project(client, "readiness-override")
    branch_id = await _main_branch_id(project_id)
    event_id = await _make_event(client, "readiness-override")
    variable = _variable(project_id, branch_id, name="variant", allowed_values=[])
    await _add(variable)
    await _add(
        VariableEventValueOverride(
            id=uuid.uuid4(),
            project_id=project_id,
            branch_id=branch_id,
            variable_id=variable.id,
            event_id=event_id,
            values=["control", "treatment"],
        )
    )

    # Act
    readiness = await _readiness(client, "readiness-override")

    # Assert
    assert readiness["variable_value_drift"] is True


@pytest.mark.asyncio
async def test_scope_readiness_ignores_variables_excluded_from_scans(
    client: AsyncClient,
) -> None:
    """An excluded variable never enters the scan's context map, so it never drifts.

    ``_event_generator_variables`` adopts-and-skips a tombstoned variable, so
    however full its documented list is, no candidate can come from it.
    """
    # Arrange
    project_id = await _make_project(client, "readiness-excluded")
    branch_id = await _main_branch_id(project_id)
    await _add(
        _variable(
            project_id,
            branch_id,
            name="retired_variant",
            allowed_values=["a", "b"],
            excluded_from_scans=True,
        )
    )

    # Act
    readiness = await _readiness(client, "readiness-excluded")

    # Assert
    assert readiness["variable_value_drift"] is False


@pytest.mark.asyncio
async def test_scope_readiness_ignores_documentation_on_a_working_branch(
    client: AsyncClient,
) -> None:
    """Detection runs against MAIN, so an unmerged branch does not make a scope live.

    This is the one way the notice could be actively wrong — telling a reader
    that nothing documents values while they are looking at a branch that does
    — so the branch rule is pinned rather than left to the reader's memory.
    """
    # Arrange
    project_id = await _make_project(client, "readiness-branch")
    working_branch_id = uuid.uuid4()
    await _add(
        PlanBranch(
            id=working_branch_id,
            project_id=project_id,
            name="document-the-variants",
            kind=BranchKind.working.value,
            status=BranchStatus.draft.value,
            description="",
        )
    )
    await _add(_variable(project_id, working_branch_id, name="variant", allowed_values=["a", "b"]))

    # Act
    readiness = await _readiness(client, "readiness-branch")

    # Assert — the same list on main would flip this to True.
    assert readiness["variable_value_drift"] is False


@pytest.mark.asyncio
async def test_scope_readiness_true_when_open_value_drifts_exist_without_documented_values(
    client: AsyncClient,
) -> None:
    """Collected rows keep THIS scope live too — the symmetric case.

    Emptying a variable's ``allowed_values`` (the obvious way to quiet the noise)
    stops new detections but closes no existing row, and the candidate builder
    reads rows, never the documented list. Without this the monitor said "cannot
    fire" about a scope that was still dispatching from the survivors.
    """
    # Arrange — the documented list is gone, the open row it produced is not.
    project_id = await _make_project(client, "readiness-value-rows")
    branch_id = await _main_branch_id(project_id)
    scan_config_id = await _make_scan(client, "readiness-value-rows", distribution_drift_fields=[])
    event_id = await _make_event(client, "readiness-value-rows")
    variable = _variable(project_id, branch_id, name="variant", allowed_values=[])
    await _add(variable)
    await _add(_value_drift_row(project_id, variable.id, event_id, scan_config_id=scan_config_id))

    # Act
    readiness = await _readiness(client, "readiness-value-rows")

    # Assert — and the scopes stay independent: no distribution row, no claim.
    assert readiness == {"variable_value_drift": True, "distribution_drift": False}


@pytest.mark.asyncio
async def test_scope_readiness_ignores_value_drifts_the_dispatcher_would_skip(
    client: AsyncClient,
) -> None:
    """A row that could never become a candidate is not readiness.

    The probe mirrors ``_get_active_variable_value_drift_candidates``: no scan
    link (the seeded demo's own shape — the loader filters on its own
    ``scan_config_id``), a resolved status, or a row past the 30-day retention
    window all leave the scope inert, and saying otherwise would replace a false
    warning with a false silence.
    """
    # Arrange
    project_id = await _make_project(client, "readiness-value-skip")
    branch_id = await _main_branch_id(project_id)
    scan_config_id = await _make_scan(client, "readiness-value-skip", distribution_drift_fields=[])
    event_id = await _make_event(client, "readiness-value-skip")
    # One variable per row: (variable_id, event_id) is unique per drift.
    unscanned = _variable(project_id, branch_id, name="unscanned", allowed_values=[])
    resolved = _variable(project_id, branch_id, name="resolved", allowed_values=[])
    aged_out = _variable(project_id, branch_id, name="aged_out", allowed_values=[])
    await _add(unscanned, resolved, aged_out)
    await _add(
        _value_drift_row(project_id, unscanned.id, event_id, scan_config_id=None),
        _value_drift_row(
            project_id,
            resolved.id,
            event_id,
            scan_config_id=scan_config_id,
            status=SCHEMA_DRIFT_STATUS_ACCEPTED,
        ),
        _value_drift_row(
            project_id,
            aged_out.id,
            event_id,
            scan_config_id=scan_config_id,
            detected_at=datetime.now(UTC) - timedelta(days=31),
        ),
    )

    # Act
    readiness = await _readiness(client, "readiness-value-skip")

    # Assert
    assert readiness["variable_value_drift"] is False


@pytest.mark.asyncio
async def test_scope_readiness_ignores_a_value_drift_on_an_excluded_variable(
    client: AsyncClient,
) -> None:
    """Excluding a variable stops its surviving rows counting, as it stops them alerting.

    The rows outlive the exclusion — excluding no longer deletes them — so the
    candidate builder asks the flag about each row's variable, and the probe has
    to ask it too or it promises a scope that the dispatcher will then filter
    down to nothing. The second act is not decoration: it proves this fixture
    can reach True at all, so the first assertion cannot pass on a mistyped
    scan or project id.
    """
    # Arrange — both lists empty, so only the collected row can carry readiness.
    project_id = await _make_project(client, "readiness-excluded-row")
    branch_id = await _main_branch_id(project_id)
    scan_config_id = await _make_scan(
        client, "readiness-excluded-row", distribution_drift_fields=[]
    )
    event_id = await _make_event(client, "readiness-excluded-row")
    excluded = _variable(
        project_id,
        branch_id,
        name="retired_variant",
        allowed_values=[],
        excluded_from_scans=True,
    )
    await _add(excluded)
    await _add(_value_drift_row(project_id, excluded.id, event_id, scan_config_id=scan_config_id))

    # Act
    readiness = await _readiness(client, "readiness-excluded-row")

    # Assert — an open, in-window, scan-linked row the dispatcher will never pick up.
    assert readiness["variable_value_drift"] is False

    # Act again — the same row shape on a variable scans still observe.
    scanned = _variable(project_id, branch_id, name="variant", allowed_values=[])
    await _add(scanned)
    await _add(_value_drift_row(project_id, scanned.id, event_id, scan_config_id=scan_config_id))

    # Assert
    assert (await _readiness(client, "readiness-excluded-row"))["variable_value_drift"] is True


@pytest.mark.asyncio
async def test_scope_readiness_true_when_a_scan_lists_distribution_fields(
    client: AsyncClient,
) -> None:
    # Arrange
    await _make_project(client, "readiness-configured")
    await _make_scan(client, "readiness-configured", distribution_drift_fields=["platform"])

    # Act
    readiness = await _readiness(client, "readiness-configured")

    # Assert
    assert readiness["distribution_drift"] is True
    assert readiness["variable_value_drift"] is False


@pytest.mark.asyncio
async def test_scope_readiness_true_when_distribution_rows_exist_without_configured_fields(
    client: AsyncClient,
) -> None:
    """Collected rows alone keep the scope live — the demo's exact shape.

    The candidate builder in ``worker/tasks/metrics/signals`` reads
    DistributionDrift ROWS, not ``ScanConfig.distribution_drift_fields``, and the
    seeded demo has rows against a config whose field list is empty. A
    "no configured fields ⇒ inert" rule would put a false warning on it.

    "Collected rows" means SIGNIFICANT ones, which is what ``_drift_row`` seeds
    and what the readiness query selects: ``metric_rows`` bands every scored PSI
    and persists the stable and minor buckets too, so a project that has only
    ever scored stable has collected plenty and can still never fire.
    The band's own arm is pinned in test_distribution_drift_readiness.py,
    which parametrises the band; this test deliberately stays the one-shape case.
    """
    # Arrange
    await _make_project(client, "readiness-collected")
    scan_config_id = await _make_scan(client, "readiness-collected", distribution_drift_fields=[])
    await _add(_drift_row(scan_config_id))

    # Act
    readiness = await _readiness(client, "readiness-collected")

    # Assert
    assert readiness["distribution_drift"] is True


@pytest.mark.asyncio
async def test_monitor_detail_reports_the_same_scope_readiness(client: AsyncClient) -> None:
    """One project fact, one shape, on both responses that render the toggles."""
    # Arrange
    project_id = await _make_project(client, "readiness-detail")
    branch_id = await _main_branch_id(project_id)
    await _make_scan(client, "readiness-detail", distribution_drift_fields=["platform"])
    await _add(_variable(project_id, branch_id, name="variant", allowed_values=[]))
    destination_resp = await client.post(
        "/api/v1/projects/readiness-detail/alert-destinations",
        json={
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert destination_resp.status_code == 201
    rule_resp = await client.post(
        f"/api/v1/projects/readiness-detail/alert-destinations/{destination_resp.json()['id']}/rules",
        json={
            "name": "Drift watch",
            "enabled": True,
            "include_distribution_drifts": True,
            "include_variable_value_drifts": True,
        },
    )
    assert rule_resp.status_code == 201

    # Act
    detail_resp = await client.get(
        f"/api/v1/projects/readiness-detail/monitors/{rule_resp.json()['id']}"
    )
    assert detail_resp.status_code == 200

    # Assert
    assert detail_resp.json()["scope_readiness"] == {
        "variable_value_drift": False,
        "distribution_drift": True,
    }
    assert detail_resp.json()["scope_readiness"] == await _readiness(client, "readiness-detail")
