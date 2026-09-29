"""Retiring the variables nothing refers to, and everything that must survive it.

The predicate deletes rows, so the interesting tests are the ones asserting it
does NOT. In particular ``test_a_variable_a_live_event_still_names_is_kept``
covers the shape that made this design conditional: on production, eighteen
variables across three projects had zero observed contexts and were still named
by a live event's field value, because a group-rule merge had deleted the event
their contexts hung off (tripl-xfxa). A predicate resting on "no contexts" alone
would delete exactly those eighteen — from a screen with a select-all checkbox
on it.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.core.analyzers._event_generator_variables import (
    VariableIndex,
    derive_display_name,
    display_name_candidates,
)
from tripl.core.variable_retirement import (
    SCAN_PROVENANCE_DESCRIPTION,
    KeptReason,
    is_json_derived,
    plan_retirement,
    referenced_tokens,
)
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value import VariableValue, VariableValueKind
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.schemas.project import VariableRetirementCounts
from tripl.tests.conftest import TestSessionLocal


def _unsaved(**overrides: object) -> Variable:
    """A variable shaped the way ``ensure_variable`` writes one, unsaved.

    ``plan_retirement`` is pure and reads only these attributes, so the pure
    tests never need a session.
    """
    variable = Variable(
        id=uuid.uuid4(),
        name="adana",
        source_name="property.Adana",
        description=SCAN_PROVENANCE_DESCRIPTION,
        bindings=["property.Adana"],
        allowed_values=[],
        excluded_from_scans=False,
    )
    for key, value in overrides.items():
        setattr(variable, key, value)
    return variable


async def _project_with_event(client: AsyncClient, slug: str, field_value: str) -> dict[str, str]:
    """A project holding one event whose single field carries *field_value*.

    The field value is the whole point: it is where ``${token}`` references
    live, and therefore what decides whether a variable is still referenced.
    """
    project = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert project.status_code == 201, project.text

    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={
            "name": "page_view",
            "display_name": "Page View",
            "field_definitions": [
                {"name": "payload", "display_name": "Payload", "field_type": "json"}
            ],
        },
    )
    assert event_type.status_code == 201, event_type.text
    event_type_id = event_type.json()["id"]

    fields = await client.get(f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields")
    assert fields.status_code == 200, fields.text
    field_id = fields.json()[0]["id"]

    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type_id,
            "name": "checkout_started",
            "field_values": [{"field_definition_id": field_id, "value": field_value}],
        },
    )
    assert event.status_code == 201, event.text
    return {
        "project_id": project.json()["id"],
        "event_type_id": event_type_id,
        "field_id": field_id,
        "event_id": event.json()["id"],
    }


async def _scan_variable(client: AsyncClient, slug: str, name: str, source_name: str) -> str:
    """A variable shaped exactly the way ``ensure_variable`` writes one."""
    response = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={
            "name": name,
            "variable_type": "string",
            "description": SCAN_PROVENANCE_DESCRIPTION,
            "bindings": [source_name],
        },
    )
    assert response.status_code == 201, response.text
    variable_id = response.json()["id"]
    # ``source_name`` is written by the scan, not by the create API, so set it
    # the way a scan would — retirement matches on it as well as on bindings.
    async with TestSessionLocal() as session:
        variable = await session.get(Variable, uuid.UUID(variable_id))
        assert variable is not None
        variable.source_name = source_name
        await session.commit()
    return variable_id


async def _preview(client: AsyncClient, slug: str) -> dict[str, int]:
    response = await client.post(
        f"/api/v1/projects/{slug}/danger/retire-unused-variables", json={"dry_run": True}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _retire(client: AsyncClient, slug: str, mode: str = "delete") -> dict[str, int]:
    response = await client.post(
        f"/api/v1/projects/{slug}/danger/retire-unused-variables",
        json={"dry_run": False, "mode": mode},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _variable_names(client: AsyncClient, slug: str, usage: str = "all") -> list[str]:
    response = await client.get(f"/api/v1/projects/{slug}/variables?usage={usage}")
    assert response.status_code == 200, response.text
    return sorted(item["name"] for item in response.json()["items"])


# --------------------------------------------------------------------------
# The pure predicate
# --------------------------------------------------------------------------


def test_every_kept_reason_the_core_can_emit_has_a_field_on_the_wire() -> None:
    """A reason the schema cannot name would vanish from the operator's preview.

    That preview is the number someone is asked to trust before pressing a
    destructive button, so every reason the predicate can produce must reach it.
    """
    emitted = {
        value
        for name, value in vars(KeptReason).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    on_the_wire = {
        field.removeprefix("kept_")
        for field in VariableRetirementCounts.model_fields
        if field.startswith("kept_")
    }
    assert emitted == on_the_wire


def test_referenced_tokens_reads_the_grammar_the_values_are_written_in() -> None:
    values = ['{"a": "${property.x}", "b": "${y}"}', "no tokens here", "${}"]
    assert referenced_tokens(values) == {"property.x", "y", ""}


def test_a_variable_with_nothing_pointing_at_it_is_retirable() -> None:
    variable = _unsaved()
    plan = plan_retirement(
        [variable], referenced=set(), with_contexts=set(), with_drifts=set(), with_overrides=set()
    )
    assert plan.retirable == [variable.id]
    assert plan.kept == {}
    assert plan.scanned == 1


def test_a_type_the_scan_inferred_does_not_keep_a_variable() -> None:
    variable = _unsaved(variable_type="json", json_schema={"type": "array"})
    plan = plan_retirement(
        [variable], referenced=set(), with_contexts=set(), with_drifts=set(), with_overrides=set()
    )
    assert plan.retirable == [variable.id]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"excluded_from_scans": True}, KeptReason.EXCLUDED),
        ({"allowed_values": ["a"]}, KeptReason.DOCUMENTED),
        ({"description": "mine now"}, KeptReason.USER_EDITED),
        ({"bindings": ["property.Adana", "extra"]}, KeptReason.USER_EDITED),
        ({"json_schema": {"type": "string", "format": "email"}}, KeptReason.USER_EDITED),
    ],
)
def test_anything_a_human_touched_is_kept_and_says_why(
    overrides: dict[str, object], reason: str
) -> None:
    variable = _unsaved(**overrides)
    plan = plan_retirement(
        [variable], referenced=set(), with_contexts=set(), with_drifts=set(), with_overrides=set()
    )
    assert plan.retirable == []
    assert plan.kept == {reason: 1}


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        ("with_contexts", KeptReason.OBSERVED),
        ("with_drifts", KeptReason.DOCUMENTED),
        ("with_overrides", KeptReason.DOCUMENTED),
    ],
)
def test_any_row_hanging_off_the_variable_keeps_it(evidence: str, reason: str) -> None:
    variable = _unsaved()
    sets: dict[str, set[uuid.UUID]] = {
        "with_contexts": set(),
        "with_drifts": set(),
        "with_overrides": set(),
    }
    sets[evidence] = {variable.id}
    plan = plan_retirement([variable], referenced=set(), **sets)
    assert plan.retirable == []
    assert plan.kept == {reason: 1}


def test_a_variable_is_matched_by_its_source_name_and_bindings_not_only_its_name() -> None:
    """576 production rows render as ``${aalter}`` over ``property.Aalter``.

    Matching the display name alone would retire a variable whose stored value
    still says ``${property.Aalter}``, because ``derive_display_name`` slugged
    the name away from the path the value was written with.
    """
    variable = _unsaved(name="aalter", source_name="property.Aalter", bindings=["property.Aalter"])
    plan = plan_retirement(
        [variable],
        referenced={"property.Aalter"},
        with_contexts=set(),
        with_drifts=set(),
        with_overrides=set(),
    )
    assert plan.retirable == []
    assert plan.kept == {KeptReason.REFERENCED: 1}


def _plan_alone(variable: Variable):
    """``plan_retirement`` over one variable with no evidence of any kind."""
    return plan_retirement(
        [variable], referenced=set(), with_contexts=set(), with_drifts=set(), with_overrides=set()
    )


# --------------------------------------------------------------------------
# The names a scan can write (tripl-bwo8)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "candidates"),
    [
        # A plain column name is written untouched, case and all.
        ("variant", ["variant"]),
        ("Variant", ["Variant"]),
        # Trailing segments, shortest first.
        ("page_data.extra.variant", ["variant", "extra_variant", "page_data_extra_variant"]),
        # The production map keys: lower-cased, punctuation and whitespace
        # collapsed to one underscore, edges trimmed.
        ("property.Albany, OR", ["albany_or", "property_albany_or"]),
        # A candidate that does not start with a letter gets the ``v_`` prefix
        # the variable-name grammar demands; the longer one already does.
        ("property.2024", ["v_2024", "property_2024"]),
        # A whitespace-only key — two of them existed on production — has no
        # one-segment candidate at all, so the column name is the first.
        ("property.  ", ["property"]),
    ],
)
def test_display_name_candidates_are_the_names_a_scan_could_write(
    token: str, candidates: list[str]
) -> None:
    assert display_name_candidates(token) == candidates


def test_derive_display_name_takes_the_first_candidate_the_index_does_not_know() -> None:
    """The refactor split the list out of ``derive_display_name``; this pins that
    the walk is unchanged — candidate order, the index check, the raw-token
    fallback — so the retirement predicate and the scan agree on what "a name
    the scan wrote" means."""
    token = "page_data.extra.variant"
    assert derive_display_name(token, VariableIndex()) == "variant"

    taken = VariableIndex([_unsaved(name="variant", source_name="variant", bindings=["variant"])])
    assert derive_display_name(token, taken) == "extra_variant"

    every_candidate_taken = VariableIndex(
        [
            _unsaved(name=name, source_name=name, bindings=[name])
            for name in display_name_candidates(token)
        ]
    )
    assert derive_display_name(token, every_candidate_taken) == token


@pytest.mark.parametrize(
    ("name", "retired"),
    [
        # The shortest candidate — what the scan writes when nothing claims it.
        ("adana", True),
        # A longer candidate — what the scan writes when ``adana`` was taken.
        # Also the documented false negative: a person who typed exactly this
        # is not detected, and the row is recycled as it was before.
        ("property_adana", True),
        # The legacy fallback: every candidate taken, raw path kept.
        ("property.Adana", True),
        # None of the above was written by the scan.
        ("turkey_city", False),
        ("Adana", False),
    ],
)
def test_a_renamed_scan_variable_belongs_to_the_person_who_renamed_it(
    name: str, retired: bool
) -> None:
    """The scan writes a display name once and never touches it again.

    Now that the scheduled sweep runs on every cycle, a JSON key that stops
    arriving for one interval is a fossil by this predicate; recycling the row
    re-derives its name, which would silently undo a rename an operator made.
    So a name outside ``display_name_candidates(source_name)`` — and not the
    raw path itself — is read as a human's, and kept as ``user_edited``.
    """
    variable = _unsaved(name=name)
    plan = _plan_alone(variable)
    if retired:
        assert plan.retirable == [variable.id]
        assert plan.kept == {}
    else:
        assert plan.retirable == []
        assert plan.kept == {KeptReason.USER_EDITED: 1}


def test_a_plain_column_variable_is_renamed_when_its_name_is_not_the_column() -> None:
    """Non-dotted tokens have one candidate — the token itself, untouched."""
    scan_written = _unsaved(name="variant", source_name="variant", bindings=["variant"])
    assert _plan_alone(scan_written).kept == {}

    renamed = _unsaved(name="experiment_arm", source_name="variant", bindings=["variant"])
    assert _plan_alone(renamed).kept == {KeptReason.USER_EDITED: 1}


def test_the_rename_arm_needs_a_source_name_to_compare_against() -> None:
    """A row without ``source_name`` has no scan-written name; the arm stays out
    of the way and the row is judged exactly as before the arm existed."""
    plan = _plan_alone(_unsaved(name="anything", source_name=None, bindings=[]))
    assert plan.kept == {}
    assert len(plan.retirable) == 1


# --------------------------------------------------------------------------
# Which variables came out of a JSON column (tripl-bwo8)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_name", "json_columns", "expected"),
    [
        # A path under a JSON column — the production shape.
        ("payload.user.adana", {"payload"}, True),
        # A longer prefix names the JSON column: a ClickHouse ``Nested`` or
        # BigQuery ``RECORD`` field is itself a dotted FieldDefinition name.
        ("nested.props.key", {"nested.props"}, True),
        # Dotted, but no proper prefix is a JSON column: a ``Nested`` scalar
        # column called ``nested.col`` — "contains a dot" is not the test.
        ("nested.col", {"other"}, False),
        # The whole name is a JSON column, not a path INSIDE one; the scan only
        # mints ``column.path`` under a JSON column, so this is not its shape.
        ("nested.col", {"nested.col"}, False),
        # A plain column, even when a JSON column shares its name.
        ("variant", {"variant"}, False),
        # Nothing at all.
        (None, {"payload"}, False),
    ],
)
def test_is_json_derived_asks_the_stored_column_type_not_the_spelling(
    source_name: str | None, json_columns: set[str], expected: bool
) -> None:
    variable = _unsaved(source_name=source_name, bindings=[source_name] if source_name else [])
    assert is_json_derived(variable, json_columns) is expected


# --------------------------------------------------------------------------
# End to end, through the endpoint
# --------------------------------------------------------------------------


async def test_a_dry_run_reports_the_counts_and_deletes_nothing(client: AsyncClient) -> None:
    slug = "retire-dry"
    await _project_with_event(client, slug, '{"kept": "${property.kept}"}')
    await _scan_variable(client, slug, "kept", "property.kept")
    await _scan_variable(client, slug, "gone", "property.gone")

    counts = await _preview(client, slug)
    assert counts["scanned"] == 2
    assert counts["retirable"] == 1
    assert counts["retired"] == 0
    assert counts["kept_referenced"] == 1
    assert await _variable_names(client, slug) == ["gone", "kept"]


async def test_a_variable_a_live_event_still_names_is_kept(client: AsyncClient) -> None:
    """The tripl-xfxa shape: referenced by a value, with no observed context.

    This is what makes the reference check load-bearing rather than
    belt-and-braces. On production it was eighteen rows, and every one of them
    would have been deleted by a predicate that only asked "does it have
    contexts?".
    """
    slug = "retire-xfxa"
    await _project_with_event(client, slug, '{"orphan": "${property.orphan}"}')
    await _scan_variable(client, slug, "orphan", "property.orphan")

    async with TestSessionLocal() as session:
        contexts = (await session.execute(select(VariableValue))).scalars().all()
    assert contexts == [], "the fixture must reproduce the bug: referenced, zero contexts"

    counts = await _retire(client, slug)
    assert counts["retired"] == 0
    assert counts["kept_referenced"] == 1
    assert await _variable_names(client, slug) == ["orphan"]


async def test_a_variable_named_only_from_a_meta_value_is_kept(client: AsyncClient) -> None:
    """Meta values are a ``${token}`` site too, and they produce no context.

    ``VariableValue`` is keyed by ``field_definition_id``, so a variable
    referenced only from an event's META value has zero contexts by
    construction. The first cut of this predicate read ``event_field_values``
    alone and would have deleted it — after which the event naming it would
    start rendering "Unknown variable token", which is precisely the outcome
    retirement is supposed to prevent.
    ``event_service._attach_template_warnings`` reads both tables; so must this.
    """
    slug = "retire-meta"
    ids = await _project_with_event(client, slug, "{}")
    await _scan_variable(client, slug, "campaign", "property.campaign")

    meta_field = await client.post(
        f"/api/v1/projects/{slug}/meta-fields",
        json={"name": "owner_note", "display_name": "Owner note", "field_type": "string"},
    )
    assert meta_field.status_code == 201, meta_field.text
    patched = await client.patch(
        f"/api/v1/projects/{slug}/events/{ids['event_id']}",
        json={
            "meta_values": [
                {
                    "meta_field_definition_id": meta_field.json()["id"],
                    "value": "owned by ${property.campaign}",
                }
            ]
        },
    )
    assert patched.status_code == 200, patched.text

    counts = await _retire(client, slug)
    assert counts["retired"] == 0
    assert counts["kept_referenced"] == 1
    assert await _variable_names(client, slug) == ["campaign"]


async def test_observed_values_drift_and_overrides_each_keep_a_variable(
    client: AsyncClient,
) -> None:
    slug = "retire-evidence"
    ids = await _project_with_event(client, slug, "{}")
    observed_id = await _scan_variable(client, slug, "observed", "property.observed")
    drifted_id = await _scan_variable(client, slug, "drifted", "property.drifted")
    overridden_id = await _scan_variable(client, slug, "overridden", "property.overridden")
    await _scan_variable(client, slug, "inert", "property.inert")

    project_id = uuid.UUID(ids["project_id"])
    event_id = uuid.UUID(ids["event_id"])
    async with TestSessionLocal() as session:
        variable = await session.get(Variable, uuid.UUID(observed_id))
        assert variable is not None
        session.add(
            VariableValue(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=variable.branch_id,
                variable_id=variable.id,
                event_id=event_id,
                field_definition_id=uuid.UUID(ids["field_id"]),
                source_column="property",
                value_kind=VariableValueKind.low.value,
                observed_count=3,
                values=["a"],
            )
        )
        session.add(
            VariableValueDrift(
                id=uuid.uuid4(),
                project_id=project_id,
                variable_id=uuid.UUID(drifted_id),
                event_id=event_id,
                observed_values=["surprise"],
            )
        )
        session.add(
            VariableEventValueOverride(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=variable.branch_id,
                variable_id=uuid.UUID(overridden_id),
                event_id=event_id,
                values=["one", "two"],
            )
        )
        await session.commit()

    counts = await _retire(client, slug)
    assert counts["retired"] == 1
    assert counts["kept_observed"] == 1
    assert counts["kept_documented"] == 2, "drift and override are both human decisions"
    assert await _variable_names(client, slug) == ["drifted", "observed", "overridden"]


async def test_retiring_is_idempotent(client: AsyncClient) -> None:
    slug = "retire-twice"
    await _project_with_event(client, slug, "{}")
    await _scan_variable(client, slug, "gone", "property.gone")

    assert (await _retire(client, slug))["retired"] == 1
    assert await _retire(client, slug) == {
        "scanned": 0,
        "retirable": 0,
        "retired": 0,
        "kept_referenced": 0,
        "kept_observed": 0,
        "kept_documented": 0,
        "kept_user_edited": 0,
        "kept_excluded": 0,
    }


async def test_exclude_mode_tombstones_instead_of_deleting(client: AsyncClient) -> None:
    """Exclusion keeps the row, so the next scan cannot mint the name again."""
    slug = "retire-exclude"
    await _project_with_event(client, slug, "{}")
    await _scan_variable(client, slug, "gone", "property.gone")

    counts = await _retire(client, slug, mode="exclude")
    assert counts["retired"] == 1
    async with TestSessionLocal() as session:
        variable = (await session.execute(select(Variable))).scalars().one()
        assert variable.excluded_from_scans is True

    # And it now keeps ITSELF: an excluded row is a deliberate instruction, so a
    # second pass must not delete the tombstone the first one just wrote.
    assert (await _retire(client, slug))["kept_excluded"] == 1


# --------------------------------------------------------------------------
# The list filter reads the same predicate
# --------------------------------------------------------------------------


async def test_the_unused_filter_returns_exactly_what_retirement_would_take(
    client: AsyncClient,
) -> None:
    """The count under a select-all checkbox has to be the retirable set itself.

    A cheaper "event_count is zero" filter would list the referenced variable
    too, which is how somebody select-alls their way into the tripl-xfxa rows.
    """
    slug = "retire-filter"
    await _project_with_event(client, slug, '{"kept": "${property.kept}"}')
    await _scan_variable(client, slug, "kept", "property.kept")
    await _scan_variable(client, slug, "gone", "property.gone")

    assert await _variable_names(client, slug, usage="unused") == ["gone"]
    assert await _variable_names(client, slug, usage="used") == ["kept"]
    assert await _variable_names(client, slug, usage="all") == ["gone", "kept"]

    preview = await _preview(client, slug)
    unused = await client.get(f"/api/v1/projects/{slug}/variables?usage=unused")
    assert unused.json()["total"] == preview["retirable"]


async def test_an_unknown_usage_value_is_a_422_not_a_500(client: AsyncClient) -> None:
    """Free-string enum params were the tripl-57g0 defect; this one is declared."""
    slug = "retire-enum"
    await _project_with_event(client, slug, "{}")
    response = await client.get(f"/api/v1/projects/{slug}/variables?usage=maybe")
    assert response.status_code == 422, response.text
