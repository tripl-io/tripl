"""JSON Schema fragments on variables (F23.2, #306)."""

from typing import Any

import pytest
from httpx import AsyncClient

from tripl.core.property_schema import (
    SCHEMA_MAX_DEPTH,
    PropertySchemaError,
    check_schema_matches_type,
    infer_property_type,
    is_scan_inferred_schema,
    validate_property_schema,
)

CART = {
    "type": "object",
    "properties": {
        "total": {"type": "number", "minimum": 0},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"sku": {"type": "string"}, "qty": {"type": "integer"}},
                "required": ["sku"],
            },
        },
        "coupon": {"type": "string", "enum": ["SPRING", "VIP"]},
    },
    "required": ["total"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "integer", "minimum": 0, "maximum": 100},
        {"type": "string", "format": "email", "maxLength": 320},
        {"type": "string", "pattern": "^[A-Z]{3}$", "description": "ISO currency"},
        {"type": "array", "items": {"type": "string"}, "uniqueItems": True, "maxItems": 10},
        {"type": "boolean"},
        CART,
    ],
)
def test_supported_fragments_are_accepted_unchanged(schema: dict[str, Any]) -> None:
    assert validate_property_schema(schema) is schema


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ([], "must be an object"),
        ({}, "'type' must be one of"),
        ({"type": ["string", "null"]}, "'type' must be one of"),
        ({"type": "string", "$ref": "#/defs/x"}, "unsupported keyword(s) for type string: $ref"),
        ({"type": "string", "minimum": 1}, "unsupported keyword(s) for type string: minimum"),
        ({"type": "string", "enum": ["a"]}, "document allowed values in 'allowed_values'"),
        ({"type": "string", "format": "credit-card"}, "'format' must be one of"),
        ({"type": "string", "pattern": "("}, "'pattern' is not a valid regex"),
        ({"type": "string", "minLength": 5, "maxLength": 2}, "'minLength' is greater"),
        ({"type": "integer", "minimum": True}, "'minimum' must be a number"),
        ({"type": "number", "maximum": float("inf")}, "plain JSON"),
        (
            {"type": "object", "properties": {"a": {"type": "number", "minimum": float("nan")}}},
            "plain JSON",
        ),
        ({"type": "number", "multipleOf": 0}, "'multipleOf' must be a positive number"),
        ({"type": "array", "minItems": -1}, "'minItems' must be a non-negative integer"),
        (
            {"type": "object", "properties": {"a": {"type": "integer", "enum": ["x"]}}},
            "json_schema.properties.a: every 'enum' value must be a integer",
        ),
        (
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]},
            "'required' names undeclared properties: b",
        ),
        (
            {"type": "object", "additionalProperties": {}},
            "'additionalProperties' must be a boolean",
        ),
        ({"type": "array", "items": {"type": "date"}}, "json_schema.items: 'type' must be one of"),
    ],
)
def test_unsupported_fragments_are_refused_by_name(schema: object, message: str) -> None:
    with pytest.raises(PropertySchemaError) as exc:
        validate_property_schema(schema)
    assert message in str(exc.value)


def test_depth_and_size_are_bounded() -> None:
    deep: dict[str, Any] = {"type": "string"}
    for _ in range(SCHEMA_MAX_DEPTH):
        deep = {"type": "array", "items": deep}
    with pytest.raises(PropertySchemaError, match="nested deeper"):
        validate_property_schema(deep)

    wide = {"type": "object", "properties": {f"p{i:04}": {"type": "string"} for i in range(900)}}
    with pytest.raises(PropertySchemaError, match="larger than"):
        validate_property_schema(wide)


@pytest.mark.parametrize(
    ("variable_type", "schema"),
    [
        ("string", {"type": "string", "format": "email"}),
        ("number", {"type": "integer"}),
        ("number", {"type": "number"}),
        ("boolean", {"type": "boolean"}),
        ("date", {"type": "string", "format": "date"}),
        ("datetime", {"type": "string", "format": "date-time"}),
        ("json", CART),
        ("json", {"type": "array", "items": {"type": "object"}}),
        ("string_array", {"type": "array", "items": {"type": "string"}}),
        ("number_array", {"type": "array", "items": {"type": "integer"}}),
        ("string", None),
    ],
)
def test_a_schema_that_agrees_with_the_type_passes(
    variable_type: str, schema: dict[str, Any] | None
) -> None:
    check_schema_matches_type(variable_type, schema)


@pytest.mark.parametrize(
    ("variable_type", "schema"),
    [
        ("string", {"type": "integer"}),
        ("string", {"type": "string", "format": "date-time"}),
        ("date", {"type": "string"}),
        ("datetime", {"type": "string", "format": "date"}),
        ("number", {"type": "string"}),
        ("json", {"type": "string"}),
        ("string_array", {"type": "array"}),
        ("string_array", {"type": "array", "items": {"type": "integer"}}),
        ("number_array", {"type": "array", "items": {"type": "string"}}),
    ],
)
def test_a_schema_that_contradicts_the_type_is_refused(
    variable_type: str, schema: dict[str, Any]
) -> None:
    with pytest.raises(PropertySchemaError, match=f"does not match variable_type {variable_type}"):
        check_schema_matches_type(variable_type, schema)


# --- API ---------------------------------------------------------------------


async def _project(client: AsyncClient, slug: str) -> None:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text


async def _variable(client: AsyncClient, slug: str, name: str, branch: str | None = None) -> dict:
    query = f"?branch={branch}" if branch else ""
    items = (await client.get(f"/api/v1/projects/{slug}/variables{query}")).json()["items"]
    return next(v for v in items if v["name"] == name)


async def _patch(
    client: AsyncClient, slug: str, var_id: str, body: dict, branch: str | None = None
) -> Any:
    query = f"?branch={branch}" if branch else ""
    return await client.patch(f"/api/v1/projects/{slug}/variables/{var_id}{query}", json=body)


@pytest.mark.asyncio
async def test_create_stores_and_returns_the_schema(client: AsyncClient) -> None:
    slug = "schema-create"
    await _project(client, slug)
    resp = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "cart", "variable_type": "json", "json_schema": CART},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["json_schema"] == CART
    assert (await _variable(client, slug, "cart"))["json_schema"] == CART

    plain = await client.post(f"/api/v1/projects/{slug}/variables", json={"name": "plan"})
    assert plain.json()["json_schema"] is None


@pytest.mark.asyncio
async def test_create_refuses_a_bad_or_contradicting_schema(client: AsyncClient) -> None:
    slug = "schema-create-bad"
    await _project(client, slug)
    for body in (
        {"name": "a", "variable_type": "string", "json_schema": {"type": "integer"}},
        {"name": "b", "variable_type": "string", "json_schema": {"type": "string", "$ref": "x"}},
    ):
        resp = await client.post(f"/api/v1/projects/{slug}/variables", json=body)
        assert resp.status_code == 422, resp.text
    listed = (await client.get(f"/api/v1/projects/{slug}/variables")).json()
    assert listed["total"] == 0


@pytest.mark.asyncio
async def test_update_judges_the_type_and_schema_as_one_pair(client: AsyncClient) -> None:
    slug = "schema-update"
    await _project(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "amount", "variable_type": "number", "json_schema": {"type": "integer"}},
    )
    var_id = created.json()["id"]

    # A retype against the stored schema is refused, and nothing changes.
    retyped = await _patch(client, slug, var_id, {"variable_type": "string"})
    assert retyped.status_code == 422
    assert "does not match variable_type string" in retyped.json()["detail"]
    # So is a schema against the stored type.
    reschema = await _patch(client, slug, var_id, {"json_schema": {"type": "boolean"}})
    assert reschema.status_code == 422
    stored = await _variable(client, slug, "amount")
    assert (stored["variable_type"], stored["json_schema"]) == ("number", {"type": "integer"})

    # Both halves in one patch are fine, and null clears the schema.
    both = await _patch(
        client, slug, var_id, {"variable_type": "string", "json_schema": {"type": "string"}}
    )
    assert both.status_code == 200, both.text
    cleared = await _patch(client, slug, var_id, {"json_schema": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["json_schema"] is None


@pytest.mark.asyncio
async def test_bulk_retype_is_refused_whole_when_a_schema_disagrees(client: AsyncClient) -> None:
    slug = "schema-bulk"
    await _project(client, slug)
    typed = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "qty", "variable_type": "number", "json_schema": {"type": "integer"}},
    )
    plain = await client.post(
        f"/api/v1/projects/{slug}/variables", json={"name": "note", "variable_type": "number"}
    )
    ids = [typed.json()["id"], plain.json()["id"]]

    resp = await client.post(
        f"/api/v1/projects/{slug}/variables/bulk-update",
        json={"variable_ids": ids, "variable_type": "string"},
    )
    assert resp.status_code == 422
    assert "qty" in resp.json()["detail"]
    assert "note" not in resp.json()["detail"]
    for name in ("qty", "note"):
        assert (await _variable(client, slug, name))["variable_type"] == "number"


async def _branch(client: AsyncClient, slug: str) -> str:
    resp = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _merge(client: AsyncClient, slug: str, branch_id: str) -> None:
    for action in ("submit", "approve"):
        resp = await client.post(
            f"/api/v1/projects/{slug}/branches/{branch_id}/transition", json={"action": action}
        )
        assert resp.status_code == 200, resp.text
    merged = await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
    assert merged.status_code == 200, merged.text


@pytest.mark.asyncio
async def test_a_branch_copies_the_schema_and_a_merge_brings_it_to_main(
    client: AsyncClient,
) -> None:
    slug = "schema-branch"
    await _project(client, slug)
    await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "cart", "variable_type": "json", "json_schema": CART},
    )
    branch_id = await _branch(client, slug)
    on_branch = await _variable(client, slug, "cart", branch_id)
    assert on_branch["json_schema"] == CART

    edited = {**CART, "required": []}
    resp = await _patch(client, slug, on_branch["id"], {"json_schema": edited}, branch_id)
    assert resp.status_code == 200, resp.text
    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/diff")).json()
    assert "json_schema" in str(diff)
    # Main is untouched until the merge.
    assert (await _variable(client, slug, "cart"))["json_schema"] == CART

    await _merge(client, slug, branch_id)
    assert (await _variable(client, slug, "cart"))["json_schema"] == edited


async def _diverge(client: AsyncClient, slug: str) -> str:
    """Base: ``qty`` a number with no schema. The branch narrows its schema to
    integer; main retypes it to string. Field by field these are edits to two
    different columns — and taken together, a string typed as an integer."""
    await _project(client, slug)
    await client.post(
        f"/api/v1/projects/{slug}/variables", json={"name": "qty", "variable_type": "number"}
    )
    branch_id = await _branch(client, slug)
    on_branch = await _variable(client, slug, "qty", branch_id)
    resp = await _patch(
        client, slug, on_branch["id"], {"json_schema": {"type": "integer"}}, branch_id
    )
    assert resp.status_code == 200, resp.text
    on_main = await _variable(client, slug, "qty")
    resp = await _patch(client, slug, on_main["id"], {"variable_type": "string"})
    assert resp.status_code == 200, resp.text
    return branch_id


@pytest.mark.asyncio
async def test_a_merge_refuses_when_both_sides_changed_either_half_of_the_type(
    client: AsyncClient,
) -> None:
    slug = "schema-merge-pair"
    branch_id = await _diverge(client, slug)

    conflicts = await client.get(f"/api/v1/projects/{slug}/branches/{branch_id}/conflicts")
    assert conflicts.status_code == 200, conflicts.text
    assert "qty" in str(conflicts.json())

    for action in ("submit", "approve"):
        await client.post(
            f"/api/v1/projects/{slug}/branches/{branch_id}/transition", json={"action": action}
        )
    merged = await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
    assert merged.status_code == 409, merged.text
    on_main = await _variable(client, slug, "qty")
    assert (on_main["variable_type"], on_main["json_schema"]) == ("string", None)


@pytest.mark.asyncio
async def test_update_from_main_asks_once_for_the_whole_type(client: AsyncClient) -> None:
    slug = "schema-ufm-conflict"
    branch_id = await _diverge(client, slug)
    url = f"/api/v1/projects/{slug}/branches/{branch_id}/update-from-main"

    blind = await client.post(url, json={})
    assert blind.status_code == 409, blind.text
    assert blind.json()["detail"]["unresolved_conflicts"] == [
        {"entity_type": "variable", "name": "qty", "field": "variable_type"}
    ]

    taken = await client.post(
        url,
        json={
            "resolutions": [
                {
                    "entity_type": "variable",
                    "entity_name": "qty",
                    "field_name": "variable_type",
                    "choice": "ours",
                }
            ]
        },
    )
    assert taken.status_code == 200, taken.text
    on_branch = await _variable(client, slug, "qty", branch_id)
    # Main's pair, both halves: its type and its (absent) schema.
    assert (on_branch["variable_type"], on_branch["json_schema"]) == ("string", None)


@pytest.mark.asyncio
async def test_update_from_main_brings_both_halves_of_mains_type(client: AsyncClient) -> None:
    slug = "schema-ufm-clean"
    await _project(client, slug)
    await client.post(
        f"/api/v1/projects/{slug}/variables", json={"name": "day", "variable_type": "string"}
    )
    branch_id = await _branch(client, slug)
    on_main = await _variable(client, slug, "day")
    schema = {"type": "string", "format": "date"}
    resp = await _patch(
        client, slug, on_main["id"], {"variable_type": "date", "json_schema": schema}
    )
    assert resp.status_code == 200, resp.text

    updated = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/update-from-main", json={}
    )
    assert updated.status_code == 200, updated.text
    on_branch = await _variable(client, slug, "day", branch_id)
    assert (on_branch["variable_type"], on_branch["json_schema"]) == ("date", schema)


@pytest.mark.asyncio
async def test_reverting_either_half_restores_both(client: AsyncClient) -> None:
    slug = "schema-revert"
    await _project(client, slug)
    await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "amount", "variable_type": "number", "json_schema": {"type": "integer"}},
    )
    branch_id = await _branch(client, slug)
    on_branch = await _variable(client, slug, "amount", branch_id)
    resp = await _patch(
        client,
        slug,
        on_branch["id"],
        {"variable_type": "string", "json_schema": {"type": "string", "format": "email"}},
        branch_id,
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/api/v1/projects/{slug}/branches/{branch_id}/revert",
        json={"entity_type": "variable", "name": "amount", "field": "json_schema"},
    )
    assert resp.status_code == 200, resp.text
    restored = await _variable(client, slug, "amount", branch_id)
    assert (restored["variable_type"], restored["json_schema"]) == ("number", {"type": "integer"})


# --- scan-time inference (F23.4) ---------------------------------------------


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["pro", "free"], ("string", None)),
        ([2, 10, None], ("number", None)),
        ([2, 9.99], ("number", None)),
        ([10.0], ("number", None)),
        ([True, False], ("boolean", None)),
        (["2026-09-29"], ("date", None)),
        (["2026-09-29T10:00:00Z", "2026-09-29"], ("datetime", None)),
        (["2026-09-29", "soon"], ("string", None)),
        ([["a", "b"], []], ("string_array", None)),
        ([[1, 2.5]], ("number_array", None)),
        ([[], []], None),
        ([[], ["a"]], ("string_array", None)),
        ([[{"sku": "x"}]], ("json", {"type": "array"})),
        ([{"a": 1}], ("json", {"type": "object"})),
        (["42", 42], None),
        ([None, None], None),
        ([], None),
    ],
)
def test_infer_property_type(values: list[object], expected: object) -> None:
    assert infer_property_type(values) == expected


def test_every_inferred_type_passes_the_agreement_check() -> None:
    """What the scan writes must be what a person could have saved."""
    for sample in (["x"], [1], [1.5], [True], ["2026-01-01"], [[1]], [["a"]], [[{}]], [{}]):
        inferred = infer_property_type(sample)
        assert inferred is not None
        variable_type, schema = inferred
        if schema is not None:
            validate_property_schema(schema)
            assert is_scan_inferred_schema(schema)
        check_schema_matches_type(variable_type, schema)
