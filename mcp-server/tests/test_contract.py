"""Contract test: every endpoint a tool wraps must exist in backend/openapi.json.

Same spirit as the backend's own test_openapi_contract.py — fail loudly the
moment the REST contract drifts under the MCP toolset.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
from pathlib import Path
from typing import Any, get_args

import pytest
from tripl_cli.api import docs, events, monitoring, search
from tripl_cli.api import variables as variables_api
from tripl_cli.api.endpoints import ALL_TEMPLATES
from tripl_cli.client import API_PREFIX

import tripl_mcp
from tripl_mcp.contract import TOOL_ENDPOINTS
from tripl_mcp.enums import DocAudience, DocScope, EventOrderBy, EventStatus, SearchEntityType
from tripl_mcp.server import build_server

OPENAPI_PATH = Path(__file__).resolve().parents[2] / "backend" / "openapi.json"
MCP_PACKAGE = Path(tripl_mcp.__file__).parent
PATH_LITERAL = re.compile(r'f?"(/(?:projects|auth|data-sources)[^"]*)"')

# Wire keys whose meaning ``tripl_cli.api`` owns: the paged envelope, search's
# semantic flag, search's truncation flag, and the array
# ``event_types.field_count`` exists to replace. Kept identical to the CLI
# suite's own list on purpose - the two are one rule.
SHARED_RESPONSE_KEYS = frozenset(
    {"items", "total", "truncated", "semantic_used", "field_definitions"}
)

# Every tool parameter the backend constrains to a closed set, as
# (tool, parameter, where the DOCUMENT declares it). The third item is read by
# ``_document_enum``: a query parameter on one of that tool's own endpoints, or
# a property of its request body. The paths are ``tripl_cli.api``'s constants,
# the same ones ``TOOL_ENDPOINTS`` names, so this map cannot point at a route
# the tool does not call.
#
# Adding a row here is what mirroring a new enum LOOKS like; the completeness
# test below is what makes leaving one out fail.
CLOSED_SET_PARAMETERS: tuple[tuple[str, str, tuple[str, str, str, str]], ...] = (
    ("list_events", "status", ("query", "get", events.LIST, "status")),
    ("list_events", "order_by", ("query", "get", events.LIST, "order_by")),
    ("search_plan", "types", ("query", "get", search.SEARCH, "types")),
    ("create_event", "status", ("body", "post", events.LIST, "status")),
    ("read_doc", "scope", ("query", "get", docs.FILE, "scope")),
    ("search_docs", "scope", ("query", "get", docs.SEARCH, "scope")),
    ("write_doc", "scope", ("query", "put", docs.FILE, "scope")),
    # The tree route takes no `scope`: it answers both roots and list_docs narrows
    # them locally. Its argument is still DocScope, so it is held to the file
    # route's own `scope` - the one the item it names is then read with.
    ("list_docs", "scope", ("query", "get", docs.FILE, "scope")),
)

# Closed sets that a wrapped route declares and NO tool parameter mirrors, each
# with the reason. Keyed by (path, kind, name). An entry here is a decision
# about the tool surface, not a fix for a failing test - and the test asserts
# every one is still real, because an exemption nobody needs is a hole nobody
# is watching.
UNMIRRORED_CLOSED_SETS: dict[tuple[str, str, str], str] = {
    (variables_api.LIST, "query", "usage"): (
        "list_variables does not offer `usage` at all - the shared builder cannot spell it, so "
        "there is no bare string here to type. website/docs/integrate/mcp-server.md says so and "
        "points at raw REST for `usage=unused`"
    ),
    (monitoring.SHADOW_EVENTS, "query", "status"): (
        "reconciliation_status takes only `slug` and merges three routes; the shadow-event "
        "triage states belong to the accept/dismiss workflow this toolset deliberately "
        "withholds (see 'Deliberately not exposed in v1')"
    ),
    (events.DETAIL, "body", "status"): (
        "update_event carries a free-form `patch` object rather than named fields, so no "
        "parameter exists to annotate. Splitting `patch` into typed arguments is a change to "
        "the tool surface and to what a partial update MEANS, not a type annotation"
    ),
}


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    assert OPENAPI_PATH.exists(), (
        f"Committed OpenAPI snapshot missing at {OPENAPI_PATH}; the contract test cannot run."
    )
    document: dict[str, Any] = json.loads(OPENAPI_PATH.read_text())
    return document


@pytest.fixture(scope="module")
def openapi_paths(openapi: dict[str, Any]) -> dict[str, Any]:
    return dict(openapi["paths"])


@pytest.fixture(scope="module")
def tool_schemas() -> dict[str, dict[str, Any]]:
    """The REAL input schema of every registered tool, as a client receives it.

    Built from ``build_server()`` rather than from the annotations, because the
    annotation is not the artefact under test: what an agent is held to is the
    JSON schema FastMCP derives from it.
    """
    return {tool.name: dict(tool.inputSchema) for tool in asyncio.run(build_server().list_tools())}


def test_every_wrapped_endpoint_exists_in_openapi(
    openapi_paths: dict[str, Any],
) -> None:
    missing: list[str] = []
    for tool, endpoints in TOOL_ENDPOINTS.items():
        for method, path in endpoints:
            full_path = f"{API_PREFIX}{path}"
            operations = openapi_paths.get(full_path)
            if operations is None or method not in operations:
                missing.append(f"{tool}: {method.upper()} {full_path}")
    assert not missing, (
        "MCP tools wrap endpoints that are no longer in backend/openapi.json "
        "(REST contract drift!):\n  " + "\n  ".join(missing)
    )


def test_every_registered_tool_has_a_contract_entry() -> None:
    mcp = build_server()
    tools = asyncio.run(mcp.list_tools())
    registered = {tool.name for tool in tools}

    assert registered == set(TOOL_ENDPOINTS), (
        "Tool registry and contract map out of sync. "
        f"Only registered: {sorted(registered - set(TOOL_ENDPOINTS))}; "
        f"only in contract: {sorted(set(TOOL_ENDPOINTS) - registered)}"
    )


def test_tool_endpoints_are_the_shared_templates() -> None:
    """Identity, not equality-by-copy.

    ``TOOL_ENDPOINTS`` names the constants ``tripl_cli.api`` defines and the tool
    bodies ``.format()``, so this map cannot describe a path the tool does not
    send - and a backend rename breaks the CLI and the MCP together rather than
    one loudly and the other silently.
    """
    unknown = [
        f"{tool}: {path}"
        for tool, endpoints in TOOL_ENDPOINTS.items()
        for _, path in endpoints
        if path not in ALL_TEMPLATES
    ]
    assert not unknown, f"TOOL_ENDPOINTS carries paths tripl_cli.api does not declare: {unknown}"


def test_list_events_exposes_every_filter_the_shared_builder_accepts() -> None:
    """A filter the builder can send and the tool cannot name is unreachable here.

    ``reviewed`` reached the route and the frontend and neither of the clients
    that share ``tripl_cli.api`` - and this tool already puts ``reviewed`` in
    every trimmed row it returns, so an agent could see the flag on each event
    and had no way to ask for one half of them. The CLI's own contract suite
    holds the builder to the route; this closes the second half of that chain.
    """
    from tripl_cli.api import events as builder

    from tripl_mcp.tools import events as tool

    # ``ctx`` is FastMCP's, ``slug`` is positional on both, and ``branch_id`` is
    # this surface's spelling of the builder's ``branch`` - the one rename, and
    # the reason this compares names rather than signatures.
    accepted = set(inspect.signature(builder.list_events).parameters) - {"slug"}
    exposed = {
        "branch" if name == "branch_id" else name
        for name in inspect.signature(tool.list_events).parameters
    } - {"slug", "ctx"}
    missing = sorted(accepted - exposed)
    assert not missing, (
        "tripl_cli.api.events.list_events can send filters list_events does not expose, "
        f"so no agent can reach them: {missing}"
    )


def _resolve(openapi: dict[str, Any], schema: Any) -> dict[str, Any]:
    """Follow ``$ref`` into ``components.schemas``; anything else passes through.

    Bounded rather than a ``while True``: a malformed document should fail this
    test, not hang the job.
    """
    for _ in range(8):
        if not (isinstance(schema, dict) and "$ref" in schema):
            break
        prefix, _, name = schema["$ref"].rpartition("/")
        # The POINTER PREFIX is checked, not discarded. This helper is handed
        # FastMCP-generated tool schemas as well as the document's own, and
        # pydantic emits ``#/$defs/X``. Taking only the last segment would resolve
        # such a ref into the BACKEND's components, so a parameter typed as a real
        # StrEnum rather than a Literal would have both sides of the comparison
        # read the same backend schema and the assertion would pass for any code
        # at all. Fail loudly instead.
        assert prefix == "#/components/schemas", (
            f"{schema['$ref']} is not a ref into this document — resolving it here would "
            "read the backend's own schema for BOTH sides of the comparison"
        )
        schema = ((openapi.get("components") or {}).get("schemas") or {}).get(name)
    return schema if isinstance(schema, dict) else {}


def _enum_members(openapi: dict[str, Any], schema: Any) -> tuple[str, ...]:
    """The closed set a schema pins its value to, or ``()``.

    Reaches through every wrapper either side of this comparison emits, which is
    the whole reason it exists: an optional parameter arrives as
    ``anyOf: [X, {"type": "null"}]``, a repeatable one as an array whose
    ``items`` carry the enum, and the document names ``EventStatus`` by ``$ref``
    where the tool schema inlines it. A naive lookup finds no ``enum`` on any of
    those four and the assertion below would pass by reading nothing from both
    sides at once.
    """
    schema = _resolve(openapi, schema)
    if "enum" in schema:
        return tuple(schema["enum"])
    for branch in schema.get("anyOf", []):
        members = _enum_members(openapi, branch)
        if members:
            return members
    return _enum_members(openapi, schema["items"]) if isinstance(schema.get("items"), dict) else ()


def _body_model(openapi: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    content = ((operation.get("requestBody") or {}).get("content") or {}).get("application/json")
    return _resolve(openapi, (content or {}).get("schema") or {})


def _document_enum(openapi: dict[str, Any], where: tuple[str, str, str, str]) -> tuple[str, ...]:
    """The values the ROUTE accepts for one query parameter or body property."""
    kind, method, path, name = where
    operation = openapi["paths"][f"{API_PREFIX}{path}"][method]
    if kind == "query":
        for parameter in operation.get("parameters", []):
            if parameter.get("name") == name and parameter.get("in") == "query":
                return _enum_members(openapi, parameter.get("schema") or {})
        return ()
    properties = _body_model(openapi, operation).get("properties") or {}
    return _enum_members(openapi, properties.get(name) or {})


def _closed_sets_on(
    openapi: dict[str, Any], method: str, path: str
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Every ``(kind, name)`` one operation closes to a fixed set of values."""
    operation = openapi["paths"][f"{API_PREFIX}{path}"][method]
    found: dict[tuple[str, str], tuple[str, ...]] = {}
    for parameter in operation.get("parameters", []):
        if parameter.get("in") != "query":
            continue
        members = _enum_members(openapi, parameter.get("schema") or {})
        if members:
            found["query", parameter["name"]] = members
    for name, schema in (_body_model(openapi, operation).get("properties") or {}).items():
        members = _enum_members(openapi, schema)
        if members:
            found["body", name] = members
    return found


def test_every_closed_set_parameter_reaches_the_tool_schema_as_an_enum(
    openapi: dict[str, Any], tool_schemas: dict[str, dict[str, Any]]
) -> None:
    """A constraint carried in prose is a constraint the client cannot enforce.

    Every one of these was annotated ``str`` and described its allowed values in
    the tool description instead, so the JSON schema said "string" and an agent
    passing ``order_by="newest"`` paid a request to be told 422 by the route -
    an error it then has to parse, on a value its own tool schema should never
    have let it emit. Typed as a ``Literal``, the enum reaches the schema and
    the rejection happens locally and for free.

    Asserted against the DOCUMENT, never against a list spelled here: a second
    copy of an enum in a test is the drift it was written to catch. This is the
    same rule and the same source of truth as the CLI's
    ``test_the_declared_enums_are_the_openapi_ones``, which holds its
    ``choices=`` to these very schemas - the argparse mirror has been pinned
    this way for three releases and this closes the other surface.

    Order-sensitive, deliberately: the tool schema's enum is what a model reads
    when it picks a value, so it should be the document's list, not a set that
    happens to agree.
    """
    wrong: list[str] = []
    for tool, parameter, where in CLOSED_SET_PARAMETERS:
        expected = _document_enum(openapi, where)
        assert expected, (
            f"read no enum at all for {tool}.{parameter} from {where} - this walk is broken, "
            "not the tool; fix the test before trusting it"
        )
        properties = tool_schemas[tool].get("properties") or {}
        assert parameter in properties, (
            f"{tool} no longer takes a {parameter!r} argument, so this row is stale"
        )
        actual = _enum_members(openapi, properties[parameter])
        if actual != expected:
            wrong.append(
                f"{tool}.{parameter}: the tool schema offers "
                f"{list(actual) if actual else 'no enum at all (a bare string)'}, "
                f"but the route accepts only {list(expected)}"
            )
    assert not wrong, (
        "MCP tool parameters do not carry the closed set their route enforces, so a bad value "
        "costs a round trip and comes back as a 422 instead of being refused by the tool "
        "schema - annotate them with the Literals in tripl_mcp.enums:\n  " + "\n  ".join(wrong)
    )


def test_no_closed_set_on_a_wrapped_route_goes_unmirrored(openapi: dict[str, Any]) -> None:
    """The map above cannot go stale behind a route that grows a new enum.

    ``order_by`` was the reported symptom and ``status`` the obvious sibling, but
    the sweep that fixed them found ``search_plan``'s ``types`` as well - eleven
    entity kinds an agent had to guess at, with the constraint written only in
    prose that elided most of the list. Trusting whoever adds the next filter to
    also add it here is the discipline this file exists to replace, so the
    routes are walked instead.

    A parameter the toolset deliberately does not offer is not a defect: it is
    named in ``UNMIRRORED_CLOSED_SETS`` with its reason, and this asserts each of
    those is still real.
    """
    mirrored = {(path, kind, name) for _, _, (kind, _, path, name) in CLOSED_SET_PARAMETERS}
    unmirrored: list[str] = []
    honoured: set[tuple[str, str, str]] = set()
    for tool, endpoints in TOOL_ENDPOINTS.items():
        for method, path in endpoints:
            for kind, name in _closed_sets_on(openapi, method, path):
                key = (path, kind, name)
                if key in mirrored:
                    continue
                if key in UNMIRRORED_CLOSED_SETS:
                    honoured.add(key)
                    continue
                unmirrored.append(f"{tool}: {method.upper()} {path} {kind} {name!r}")
    assert not unmirrored, (
        "a route a tool wraps closes a value to a fixed set that no tool parameter mirrors - "
        "add a Literal to tripl_mcp.enums and a row to CLOSED_SET_PARAMETERS, or record why "
        f"the toolset withholds it in UNMIRRORED_CLOSED_SETS: {sorted(unmirrored)}"
    )
    assert honoured == set(UNMIRRORED_CLOSED_SETS), (
        "an entry in UNMIRRORED_CLOSED_SETS no longer matches any route; delete it: "
        f"{sorted(set(UNMIRRORED_CLOSED_SETS) - honoured)}"
    )


def test_the_mirrored_enums_are_the_cli_s_own() -> None:
    """One route, one vocabulary - across both distributions that call it.

    ``tripl_cli.api`` already transcribes these three for the CLI's ``choices=``,
    and ``tripl_mcp.enums`` transcribes them again because a ``Literal``'s
    members must be static: ``Literal[*events.STATUSES]`` is not a type, so no
    import removes the second copy. What CAN be removed is the chance of the two
    disagreeing, which is this repository's signature defect - one fact, spelled
    once per caller - aimed at the value set an agent and an operator are held
    to on the very same route.

    The test above already pins tripl_mcp to the document, and the CLI's suite
    pins tripl_cli to it; this asserts the two directly, so a half-mirrored
    widening fails with a message naming both sides rather than as two unrelated
    failures in two jobs.
    """
    disagree = []
    for name, literal, shared, source in (
        ("EventStatus", EventStatus, events.STATUSES, "api.events.STATUSES"),
        ("EventOrderBy", EventOrderBy, events.ORDER_BY, "api.events.ORDER_BY"),
        (
            "SearchEntityType",
            SearchEntityType,
            search.ENTITY_TYPES,
            "api.search.ENTITY_TYPES",
        ),
        ("DocScope", DocScope, docs.SCOPES, "api.docs.SCOPES"),
        ("DocAudience", DocAudience, docs.AUDIENCES, "api.docs.AUDIENCES"),
    ):
        mirrored = get_args(literal)
        if mirrored != tuple(shared):
            disagree.append(
                f"tripl_mcp.enums.{name} is {list(mirrored)} but tripl_cli.{source} "
                f"is {list(shared)}"
            )
    assert not disagree, (
        "the MCP tool schemas and the CLI's argparse choices mirror one route with two "
        "vocabularies:\n  " + "\n  ".join(disagree)
    )


def test_no_rest_path_literal_lives_in_tripl_mcp() -> None:
    """No tool may spell a path; it asks ``tripl_cli.api`` for one.

    The CLI's own test_contract.py scans this package too - kept here as well so
    the mcp-server job fails on its own rather than only in the sibling's CI.
    """
    offenders: list[str] = []
    for source in sorted(MCP_PACKAGE.rglob("*.py")):
        for path in PATH_LITERAL.findall(source.read_text(encoding="utf-8")):
            if path not in ALL_TEMPLATES:
                offenders.append(f"{source.name}: {path}")
    assert not offenders, (
        "tripl_mcp spells REST paths of its own - add a builder to tripl_cli.api instead: "
        f"{sorted(offenders)}"
    )


def test_no_tool_constructs_an_api_request_of_its_own() -> None:
    """Requests are built in ``tripl_cli/api`` and nowhere else."""
    offenders = [
        f"{source.name}:{node.lineno}"
        for source in sorted(MCP_PACKAGE.rglob("*.py"))
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ApiRequest"
    ]
    assert not offenders, f"tripl_mcp constructs ApiRequest directly: {offenders}"


def _read_key(node: ast.Call | ast.Subscript) -> str | None:
    """The wire key this node READS, or ``None``.

    ``data.get("items")`` and ``data["items"]`` only. Minting a key in a response
    envelope is the opposite act and every tool here does it deliberately, and so
    is testing for PRESENCE - ``"items" in data`` is how ``summarize_collection``
    tells a page from an object to pass through, a question the shared layer
    cannot answer because an absent envelope and an empty one unwrap the same.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value
    return None


def test_no_tool_re_derives_a_shared_response_fact() -> None:
    """A tool asks ``tripl_cli.api`` what a response MEANS, not just where to send.

    Sharing request building and left response reading behind, so
    three tool bodies unwrapped ``{items, total}`` themselves while the CLI
    unwrapped it through ``model.page_items`` - two readings of one wire format,
    on the routes both surfaces call. The CLI's own test_contract.py scans this
    package too; kept here as well so the mcp-server job fails on its own rather
    than only in the sibling's CI, exactly like the path-literal rule above.

    This does NOT cover the field projections. ``EVENT_LIST_FIELDS`` and friends
    are context-budget policy for a model that pays per token, they have one
    caller, and all four were left here deliberately.
    """
    offenders: list[str] = []
    for source in sorted(MCP_PACKAGE.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call | ast.Subscript):
                continue
            key = _read_key(node)
            if key in SHARED_RESPONSE_KEYS:
                offenders.append(f"{source.name}:{node.lineno} reads {key!r}")
    assert not offenders, (
        "tripl_mcp reads a response key tripl_cli.api already answers - call "
        "page_items/page_total, search.semantic_used, search.truncated or "
        f"event_types.field_count instead of a second `.get`: {sorted(offenders)}"
    )


def test_write_tools_are_not_marked_read_only() -> None:
    mcp = build_server()
    tools = asyncio.run(mcp.list_tools())
    write_tools = {"create_event", "update_event", "trigger_scan", "write_doc"}
    for tool in tools:
        assert tool.annotations is not None, f"{tool.name} lacks annotations"
        expected_read_only = tool.name not in write_tools
        assert tool.annotations.readOnlyHint is expected_read_only, tool.name


DOCS_PATH = Path(__file__).resolve().parents[2] / "website" / "docs" / "integrate" / "mcp-server.md"

# The rows of the "Enumerated arguments" table, by the argument each names.
_DOC_ENUM_ROW = re.compile(
    r"^\|\s*`(status|order_by|types|scope|audience)`\s*\|[^|]*\|(.*)\|\s*$", re.MULTILINE
)


def _documented_enum_values(argument: str) -> tuple[str, ...]:
    """The values that table publishes for one argument, in order.

    The cell is prose as well as values — the ``order_by`` row explains what each
    choice means — so the values are read as the inline-code spans, which is what
    a reader scans for and the only part that has to match the schema.
    """
    doc = DOCS_PATH.read_text(encoding="utf-8")
    for match in _DOC_ENUM_ROW.finditer(doc):
        if match.group(1) == argument:
            return tuple(re.findall(r"`([a-z_0-9]+)`", match.group(2)))
    raise AssertionError(
        f"no row for `{argument}` in the Enumerated arguments table of {DOCS_PATH}"
    )


def test_the_published_enum_table_is_the_one_the_tools_carry() -> None:
    """The docs table says it cannot drift. Nothing was making that true.

    website/docs/integrate/mcp-server.md publishes the seven statuses, the two
    order_by values and the eleven entity kinds by hand, and then tells the
    reader they are "checked against backend/openapi.json by the MCP server's
    contract test rather than kept in step by hand, so they cannot drift". No
    test read that page. So the day the backend gains a status, the sibling
    tests here go red and force `enums.py` to be updated, and the table stays
    behind — still asserting to an integrator that it is current, which is worse
    than a table making no promise at all.

    This is the promise. The repo pins doc tables this way elsewhere
    (cli/tests/test_contract.py for the finding codes, test_cli_constant_mirror
    for the backoff table); the page is now held to the same rule it claims.
    """
    for argument, literal in (
        ("status", EventStatus),
        ("order_by", EventOrderBy),
        ("types", SearchEntityType),
        ("scope", DocScope),
        ("audience", DocAudience),
    ):
        assert _documented_enum_values(argument) == get_args(literal), (
            f"the `{argument}` row of the Enumerated arguments table in {DOCS_PATH.name} "
            f"no longer matches tripl_mcp.enums, which the sibling tests hold to "
            f"backend/openapi.json — update the table"
        )
