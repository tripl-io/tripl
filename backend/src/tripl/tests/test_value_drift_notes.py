"""Regression coverage for value-drift action notes."""

import pytest
from httpx import AsyncClient

from tripl.tests.test_variable_value_drift import _insert_drift, _seed


@pytest.mark.asyncio
async def test_action_preserves_omitted_note_and_clears_explicit_null(client: AsyncClient) -> None:
    variable_id, event_id = await _seed(client, "vvd-note-semantics")
    drift_id = await _insert_drift(variable_id, event_id, ["x"])
    path = f"/api/v1/projects/vvd-note-semantics/variables/drifts/{drift_id}/action"

    snoozed = await client.post(
        path,
        json={
            "action": "snooze",
            "snoozed_until": "2030-01-01T00:00:00Z",
            "note": "wait for rollout",
        },
    )
    assert snoozed.status_code == 200
    assert snoozed.json()["resolution_note"] == "wait for rollout"

    false_positive = await client.post(path, json={"action": "false_positive"})
    assert false_positive.status_code == 200
    assert false_positive.json()["resolution_note"] == "wait for rollout"

    cleared = await client.post(path, json={"action": "false_positive", "note": None})
    assert cleared.status_code == 200
    assert cleared.json()["resolution_note"] is None

    noted = await client.post(path, json={"action": "false_positive", "note": "sample data"})
    assert noted.json()["resolution_note"] == "sample data"

    reopened = await client.post(path, json={"action": "reopen"})
    assert reopened.status_code == 200
    assert reopened.json()["resolution_note"] is None
