"""The plan-settings vocabulary the frontend shows as written.

The audit action catalog, as the frontend's label test sees it:

``frontend/src/pages/settings/auditActionCatalog.fixture.json`` is a copy of
``audit_actions.action_catalog()``. ``auditSentences.catalog.test.ts`` runs every code in
it through ``actionSentence`` and fails on one that would read as a humanised
code ("Values clear variable") or that two options would share. That test can
only hold the backend's vocabulary to it if the copy is the backend's
vocabulary, which is what this file checks: a new recorded action fails here
until the copy is regenerated, and then fails there until it has a sentence.

And the dependency edge sentences, which the Used by lists print verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tripl.services import _dependency_edges, audit_actions

_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "frontend/src/pages/settings/auditActionCatalog.fixture.json"
)

_REGENERATE = (
    'cd backend && uv run python -c "import json; '
    "from tripl.services.audit_actions import action_catalog; "
    "print(json.dumps(action_catalog().model_dump(mode='json'), indent=2))\" "
    "> ../frontend/src/pages/settings/auditActionCatalog.fixture.json"
)


def _fixture() -> object:
    if not _FIXTURE.is_file():  # pragma: no cover - only in a partial checkout
        pytest.skip(f"frontend catalog fixture not present at {_FIXTURE}")
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_frontend_catalog_fixture_matches_the_served_catalog() -> None:
    assert _fixture() == audit_actions.action_catalog().model_dump(mode="json"), (
        "auditActionCatalog.fixture.json has drifted from "
        "audit_actions.action_catalog(), so the frontend test that gives every "
        f"action a readable label is checking a stale list. Regenerate it:\n  {_REGENERATE}"
    )


def test_property_actions_are_grouped_under_the_word_the_app_uses() -> None:
    # The Action filter renders these labels as option groups; every other
    # screen calls a ``variable`` a property.
    labels = [label for label, _ in audit_actions.PROJECT_GROUPS]
    assert "Properties" in labels
    assert "Variables" not in labels
    properties = dict(audit_actions.PROJECT_GROUPS)["Properties"]
    assert all(action.startswith("variable.") for action in properties)


def _edge_sentences() -> dict[str, str]:
    """Every sentence an edge can carry: the module's upper-case string constants."""
    return {
        name: value
        for name, value in vars(_dependency_edges).items()
        if name.isupper() and isinstance(value, str)
    }


def test_edge_sentences_call_a_variable_a_property() -> None:
    # A property's Used by list read "event uses variable in a field value"
    # eleven times on a page headed PLAN · PROPERTY.
    sentences = _edge_sentences()
    assert sentences["VARIABLE_BOUND_TO_FIELD"] == "property bound to field"
    assert sentences["VARIABLE_IN_EVENT_VALUE"] == "event uses property in a field value"
    leaking = {name: text for name, text in sentences.items() if "variable" in text.lower()}
    assert leaking == {}
