"""Alerting contract and service regressions from batch 08."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tripl.alerting_validation import validate_slack_webhook_url
from tripl.models.alert_rule import AlertRule
from tripl.models.base import UtcDateTime
from tripl.models.domain_enums import AnomalyDirection, MetricScopeType
from tripl.schemas.alerting import AlertDestinationCreate, SimulatedRuleFiring
from tripl.services import _alerting_deliveries, _alerting_test_send
from tripl.worker.tasks import alerts


@pytest.mark.parametrize(
    ("field", "length"),
    [
        ("chat_id", 255),
        ("email_subject_template", 500),
        ("jira_auth_email", 255),
        ("jira_project_key", 64),
        ("jira_issue_type", 64),
        ("linear_team_id", 64),
        ("linear_state_id", 64),
    ],
)
def test_channel_input_rejects_values_wider_than_storage(field: str, length: int) -> None:
    with pytest.raises(ValidationError) as raised:
        AlertDestinationCreate.model_validate(
            {
                "type": "webhook",
                "name": "test",
                "target_url": "https://example.com/hook",
                field: "x" * (length + 1),
            }
        )
    assert any(error["loc"] == (field,) for error in raised.value.errors())


def test_rule_mute_column_restores_utc_on_sqlite() -> None:
    assert isinstance(AlertRule.__table__.c.muted_until.type, UtcDateTime)


def test_slack_host_error_lists_both_supported_hosts() -> None:
    with pytest.raises(ValueError) as raised:
        validate_slack_webhook_url("https://slack.com/api/chat.postMessage")
    assert "hooks.slack.com" in str(raised.value)
    assert "hooks.slack-gov.com" in str(raised.value)


def test_replay_firing_identifies_its_scan() -> None:
    scan_id = uuid.uuid4()
    firing = SimulatedRuleFiring(
        anomaly_id=uuid.uuid4(),
        scan_config_id=scan_id,
        scope_type=MetricScopeType.event,
        scope_ref="event-1",
        scope_name="Event 1",
        event_type_id=None,
        event_id=None,
        bucket=datetime(2026, 9, 23, tzinfo=UTC),
        direction=AnomalyDirection.spike,
        actual_count=2,
        expected_count=1,
        absolute_delta=1,
        percent_delta=100,
    )
    assert firing.model_dump(mode="json")["scan_config_id"] == str(scan_id)


@pytest.mark.asyncio
async def test_retry_disabled_destination_returns_conflict_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    destination_id = uuid.uuid4()
    session = SimpleNamespace(
        scalar=AsyncMock(
            return_value=SimpleNamespace(
                project_id=project_id,
                status="failed",
                destination_id=destination_id,
            )
        ),
        get=AsyncMock(return_value=SimpleNamespace(enabled=False)),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        _alerting_deliveries,
        "_get_project",
        AsyncMock(return_value=SimpleNamespace(id=project_id)),
    )
    with pytest.raises(HTTPException) as raised:
        await _alerting_deliveries.retry_delivery(
            session,
            "project",
            uuid.uuid4(),  # type: ignore[arg-type]
        )
    assert raised.value.status_code == 409
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_test_send_uses_canonical_egress_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    project = SimpleNamespace(id=uuid.uuid4(), is_demo=True)
    destination = SimpleNamespace(id=uuid.uuid4(), name="Slack example", type="slack")
    monkeypatch.setattr(_alerting_test_send, "_get_project", AsyncMock(return_value=project))
    monkeypatch.setattr(
        _alerting_test_send,
        "get_destination",
        AsyncMock(return_value=destination),
    )

    def refuse(_destination: object, _project: object) -> None:
        raise ValueError("policy says no egress")

    monkeypatch.setattr(alerts, "_assert_egress_allowed", refuse)
    outcome = await _alerting_test_send.send_destination_test(
        SimpleNamespace(),
        "demo",
        destination.id,  # type: ignore[arg-type]
    )
    assert outcome.response.ok is False
    assert outcome.response.error == (
        "Demo projects cannot send external alerts. policy says no egress"
    )
