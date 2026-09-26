"""Contract test: every endpoint the diagnostics read must exist in the API.

The sibling package that shares this very client already has one
(``mcp-server/tests/test_contract.py``), so before this a backend route rename
broke tripl-mcp's CI loudly and ``tripl doctor`` silently - the CLI would simply
start reporting 404s as instance faults, which is the one failure mode a
diagnostic tool must not have.

Reads the committed snapshot rather than importing the backend: the cli package
has no backend dependency, and CI runs its job from ``cli/`` with the whole
repository checked out.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest

import tripl_cli
from tripl_cli.api.endpoints import ALL_TEMPLATES, SHARED_ENDPOINTS
from tripl_cli.client import API_PREFIX
from tripl_cli.diagnostics import checks, collect, scan_checks
from tripl_cli.diagnostics.endpoints import (
    ANNOTATE_ENDPOINTS,
    DOCTOR_ENDPOINTS,
    DRIFTS_ENDPOINTS,
    EVENTS_ENDPOINTS,
    PLAN_ENDPOINTS,
    SCANS_ENDPOINTS,
    STATUS_ENDPOINTS,
    WATCH_ENDPOINTS,
)
from tripl_cli.watch import collect as watch_collect

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPO_ROOT / "backend" / "openapi.json"
DOCS_PATH = REPO_ROOT / "website" / "docs" / "run" / "cli.md"
DEPLOYMENT_DOCS_PATH = REPO_ROOT / "website" / "docs" / "run" / "deployment.md"

# The only difference between the repository's compose.yaml and the copy
# `tripl install` ships. A literal, so editing compose.yaml fails this test
# loudly instead of silently changing what a fresh install gets. See
# test_the_packaged_compose_matches_the_repo_compose for why it is removed.
MCP_BUILD_BLOCK = """    # Context is the repo root, not ./mcp-server: tripl-mcp builds against the
    # sibling `tripl` package in ./cli, which a mcp-server/ context cannot see
    # (tripl-ey6j.1).
    build:
      context: .
      dockerfile: mcp-server/Dockerfile
"""
COLLECT_SOURCES = (Path(collect.__file__), Path(watch_collect.__file__))
RULE_SOURCES = (Path(checks.__file__), Path(scan_checks.__file__))
DIAGNOSTICS_PACKAGE = Path(collect.__file__).parent

# Everything ``tripl_cli/diagnostics`` is allowed to contain, as a closed set.
# ``collect`` reads and ``checks``/``scan_checks`` judge; ``endpoints`` is the
# per-section map this file checks against the OpenAPI document. The `--json`
# builders, the ASCII renderers and the shared vocabulary sit at the package
# root instead (tripl-azhh) - only ``doctor`` reaches a verdict, so holding the
# scans/drifts/status/install documents under this name made the name a false
# claim about most of what it held. Adding an entry here is a design decision,
# not a fix for a failing test.
VERDICT_MODULES = frozenset({"__init__", "collect", "checks", "scan_checks", "endpoints"})

# Every document this CLI emits carries this key - report.py's own stability
# section says so - which makes minting it the one greppable proof of "how many
# places build a tripl document". The colon is load-bearing: it matches the dict
# key and not the many docstrings that discuss the field in prose.
ENVELOPE_MINT = '"schema_version":'

# Both packages, because the request layer is shared and a rule that applied to
# only one of them could not be checked mechanically (tripl-ey6j.5). mcp-server
# keeps its own half of this test too, so its job fails on its own.
CLI_PACKAGE = Path(tripl_cli.__file__).parent
MCP_PACKAGE = REPO_ROOT / "mcp-server" / "src" / "tripl_mcp"
SHARED_LAYER = CLI_PACKAGE / "api"

# Keyed by "<group>.<section>" rather than by section alone. A flat merge of the
# seven maps drops every duplicate key silently — "selection" alone appears in
# five of them, and `events`/`plan` both declare "fields" and "branches" — so a
# section could stop being checked without anything failing. It also makes the
# failure message name the COMMAND that would have gone blind, which is the
# whole reason endpoints.py groups them at all (tripl-3ixs).
DECLARED = {
    f"{group}.{section}": endpoints
    for group, group_map in (
        ("doctor", DOCTOR_ENDPOINTS),
        ("status", STATUS_ENDPOINTS),
        ("watch", WATCH_ENDPOINTS),
        ("scans", SCANS_ENDPOINTS),
        ("drifts", DRIFTS_ENDPOINTS),
        ("events", EVENTS_ENDPOINTS),
        ("plan", PLAN_ENDPOINTS),
        ("annotate", ANNOTATE_ENDPOINTS),
    )
    for section, endpoints in group_map.items()
}

# Path literals as they appear in the calls: "/x" or f"/x/{slug}/y".
PATH_LITERAL = re.compile(r'f?"(/(?:projects|auth|data-sources)[^"]*)"')

# Wire keys whose MEANING the shared layer owns, not just their spelling.
# `items`/`total` are the paged envelope seven routes answer; `semantic_used`
# says whether search's scores are semantic or substring; `truncated` says
# whether search dropped ranked hits, which `total` cannot answer because it is
# `len(items)` (tripl-wkwv.3); `field_definitions` is the array `field_count`
# exists to replace. Each was read at its own call site in both distributions
# until tripl-i1dt, and the copies had already diverged - `tripl events list`
# dropped a non-dict row where the MCP's `list_events` kept it, reading the very
# same response.
SHARED_RESPONSE_KEYS = frozenset(
    {"items", "total", "truncated", "semantic_used", "field_definitions"}
)

# Where those keys are read, package-relative. `model.py` holds the envelope
# readers (re-exported through `tripl_cli.api`, see below) and the two `api`
# modules hold the route-specific ones. Adding a fourth entry here is a decision
# about where a fact lives, not a fix for a failing test.
RESPONSE_FACT_SOURCES = frozenset({"model.py", "api/search.py", "api/event_types.py"})

# Every method that puts bytes on the wire, on an httpx client or on the shared
# TriplClient. Not a hand-picked three: `request` is what `api.request.send`
# itself calls, and `put`/`delete` exist on the API this CLI writes to.
CLIENT_METHODS = frozenset(
    {"request", "send", "stream", "get", "post", "put", "patch", "delete", "head", "options"}
)

# The only places allowed to touch a client outside tripl_cli/api, as
# (package-relative path, enclosing function). ``None`` means the whole module.
# Both are deliberate and both are documented at the call site; adding a third
# entry here is a design decision, not a fix for a failing test.
TRANSPORT_EXEMPTIONS: frozenset[tuple[str, str | None]] = frozenset(
    {
        # It IS the transport: the one TriplClient both distributions share.
        ("client.py", None),
        # The unauthenticated /health probe, which is outside /api/v1 and
        # deliberately carries no Authorization header (see its docstring).
        ("diagnostics/collect.py", "probe_health"),
        # The unauthenticated /auth/status probe. Same regime, and unauthenticated
        # by NECESSITY rather than by choice: `tripl install` reads it on an
        # instance that has no accounts yet, so no API key can exist to send
        # (tripl-ey6j.3). Its path still comes from api.auth.
        ("diagnostics/collect.py", "probe_auth_status"),
    }
)
# The f-string placeholder names some call sites use, mapped to the backend's
# own, which is what the declaration and the OpenAPI document both use.
PLACEHOLDER_RENAMES = {"{type_id}": "{event_type_id}", "{config_id}": "{scan_id}"}


def _path_literals(root: Path) -> dict[str, list[str]]:
    """Every REST path literal under ``root``, normalised, keyed by path."""
    found: dict[str, list[str]] = {}
    for source in sorted(root.rglob("*.py")):
        for raw in PATH_LITERAL.findall(source.read_text(encoding="utf-8")):
            path = raw
            for actual, declared in PLACEHOLDER_RENAMES.items():
                path = path.replace(actual, declared)
            found.setdefault(path, []).append(str(source.relative_to(root)))
    return found


def _is_client_receiver(node: ast.expr) -> bool:
    """Is ``x`` in ``x.get(...)`` a client? Name or attribute, either package.

    Anything spelled ``*client`` (``client``, ``http_client``,
    ``self._http_client``) plus ``api`` / ``_api``, which is what ``Reader``
    calls the ``TriplClient`` it holds.
    """
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    else:
        return False
    return name.lower().endswith("client") or name.lstrip("_") == "api"


def _enclosing_function(tree: ast.AST, lineno: int) -> str | None:
    """The innermost function containing ``lineno``, so an exemption can be
    scoped to one function rather than to a whole module."""
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.lineno <= lineno <= (node.end_lineno or node.lineno) and (
            best is None or node.lineno > best.lineno
        ):
            best = node
    return best.name if best is not None else None


def _matching_exemption(relative: str, function: str | None) -> tuple[str, str | None] | None:
    for exemption in ((relative, None), (relative, function)):
        if exemption in TRANSPORT_EXEMPTIONS:
            return exemption
    return None


def _read_key(node: ast.Call | ast.Subscript) -> str | None:
    """The wire key this node READS, or ``None``.

    Reads only: ``payload.get("items")`` and ``payload["items"]``. MINTING a key
    - ``{"items": rows}``, ``document["items"] = rows`` - is the opposite act and
    every document builder in both packages does it deliberately. So is asking
    whether a key is PRESENT (``"items" in data``), which is how
    ``summarize_collection`` tells a page from an object it should pass through:
    that question has no shared answer, because an absent envelope and an empty
    one unwrap identically.
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


def _shared_fact_reads(root: Path, allowed: frozenset[str]) -> list[str]:
    """Every read of a ``SHARED_RESPONSE_KEYS`` key outside its definition site."""
    offenders: list[str] = []
    for source in sorted(root.rglob("*.py")):
        relative = source.relative_to(root).as_posix()
        if relative in allowed:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call | ast.Subscript):
                continue
            key = _read_key(node)
            if key in SHARED_RESPONSE_KEYS:
                offenders.append(f"{relative}:{node.lineno} reads {key!r}")
    return offenders


def _raised_finding_codes() -> set[str]:
    """Every literal ``code=`` on a ``Finding(...)`` in the rule modules."""
    found: set[str] = set()
    for path in RULE_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Finding"):
                continue
            for keyword in node.keywords:
                if keyword.arg == "code" and isinstance(keyword.value, ast.Constant):
                    found.add(keyword.value.value)
    return found


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


def _query_schema(operation: dict[str, Any], name: str) -> dict[str, Any]:
    """One query parameter's schema, reaching through the ``anyOf`` FastAPI emits.

    An optional bounded int is serialised as ``anyOf: [{integer, maximum}, {null}]``
    rather than as a flat schema, so a naive lookup finds no ``maximum`` and the
    assertion below would pass by reading nothing.
    """
    for parameter in operation.get("parameters", []):
        if parameter.get("name") != name or parameter.get("in") != "query":
            continue
        schema = dict(parameter.get("schema") or {})
        for branch in schema.get("anyOf", []):
            if isinstance(branch, dict) and branch.get("type") != "null":
                merged = {**branch, **{k: v for k, v in schema.items() if k != "anyOf"}}
                return merged
        return schema
    return {}


def test_every_endpoint_read_exists_in_openapi(openapi_paths: dict[str, Any]) -> None:
    missing: list[str] = []
    for section, endpoints in DECLARED.items():
        for method, path in endpoints:
            operations = openapi_paths.get(f"{API_PREFIX}{path}")
            if operations is None or method not in operations:
                missing.append(f"{section}: {method.upper()} {API_PREFIX}{path}")
    assert not missing, (
        "tripl doctor/status/watch read endpoints that are no longer in backend/openapi.json "
        "(REST contract drift!):\n  " + "\n  ".join(missing)
    )


def test_every_path_in_collect_is_declared() -> None:
    """The declaration cannot go stale behind a newly added read.

    Scans the source for request-path literals, because the alternative - trusting
    that whoever adds a call also adds it here - is exactly the discipline this
    file exists to replace. Both collectors are scanned: watch has its own,
    deliberately, so a change to the follow loop cannot escape the contract.
    """
    source = "\n".join(path.read_text(encoding="utf-8") for path in COLLECT_SOURCES)
    found = set(PATH_LITERAL.findall(source))
    normalised = set()
    for path in found:
        for actual, declared in PLACEHOLDER_RENAMES.items():
            path = path.replace(actual, declared)
        normalised.add(path)
    declared_paths = {path for endpoints in DECLARED.values() for _, path in endpoints}
    undeclared = normalised - declared_paths
    assert not undeclared, (
        "a collect module reads paths that endpoints.py does not declare, so the contract "
        f"test cannot see them: {sorted(undeclared)}"
    )


def test_every_shared_endpoint_exists_in_openapi(openapi_paths: dict[str, Any]) -> None:
    """The shared request layer, checked whole - including the builders no CLI
    command calls, because tripl-mcp calls them and both packages read this map."""
    missing: list[str] = []
    for resource, endpoints in SHARED_ENDPOINTS.items():
        for method, path in endpoints:
            operations = openapi_paths.get(f"{API_PREFIX}{path}")
            if operations is None or method not in operations:
                missing.append(f"{resource}: {method.upper()} {API_PREFIX}{path}")
    assert not missing, (
        "tripl_cli.api declares endpoints that are no longer in backend/openapi.json "
        "(REST contract drift!):\n  " + "\n  ".join(missing)
    )


def test_no_rest_path_literal_lives_outside_the_shared_layer() -> None:
    """A module cannot spell a path the shared layer does not declare.

    Scans BOTH packages: this is one half of the acceptance criterion of
    tripl-ey6j.5 ("no request-building logic is duplicated between the CLI and
    the MCP tools") turned into something CI can check. Literals in finding
    messages and evidence keys pass, because they are the same templates - what
    fails is inventing a second spelling anywhere.
    """
    offenders: list[str] = []
    for package, root in (("tripl_cli", CLI_PACKAGE), ("tripl_mcp", MCP_PACKAGE)):
        for path, sources in _path_literals(root).items():
            if path not in ALL_TEMPLATES:
                offenders.append(f"{package}: {path} (in {', '.join(sorted(set(sources)))})")
    assert not offenders, (
        "REST path literals that tripl_cli.api does not declare - add a builder there "
        "rather than a second spelling here:\n  " + "\n  ".join(sorted(offenders))
    )


def test_api_request_is_constructed_only_in_the_shared_layer() -> None:
    """The other half of the acceptance criterion, and the load-bearing one.

    A module that can build an ``ApiRequest`` can build a request; if only
    ``tripl_cli/api`` can, then every request in either distribution came from
    the one place, by construction rather than by review. Do not relax this.
    """
    offenders: list[str] = []
    for package, root in (("tripl_cli", CLI_PACKAGE), ("tripl_mcp", MCP_PACKAGE)):
        for source in sorted(root.rglob("*.py")):
            if source.parent == SHARED_LAYER:
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ApiRequest":
                    offenders.append(f"{package}: {source.name}:{node.lineno}")
    assert not offenders, (
        "ApiRequest is constructed outside tripl_cli/api, so a request can be built "
        f"without going through the shared layer: {sorted(offenders)}"
    )


def test_nothing_outside_the_shared_layer_calls_a_client_directly() -> None:
    """The remaining door, closed on every method rather than three of them.

    A direct client call slips past both tests above - no path literal if the
    path came from a variable, and no ``ApiRequest`` either - so it has to be
    caught by the call itself. The predecessor of this test checked only
    ``get``/``post``/``patch`` on a receiver literally named ``client``, which
    missed ``request``: the very method ``api.request.send`` calls, and the one
    a copy-paste of it would use. ``delete`` and ``put`` were missing too, and
    the CLI reaches an editor-gated API where both exist.

    Scanned in BOTH packages, because the request layer is shared and a rule
    enforced on only one of them is not a rule.
    """
    offenders: list[str] = []
    honoured: set[tuple[str, str | None]] = set()
    for package, root in (("tripl_cli", CLI_PACKAGE), ("tripl_mcp", MCP_PACKAGE)):
        for source in sorted(root.rglob("*.py")):
            if source.parent == SHARED_LAYER:
                continue  # the shared layer is the one thing allowed to send
            relative = source.relative_to(root).as_posix()
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in CLIENT_METHODS:
                    continue
                if not _is_client_receiver(node.func.value):
                    continue
                exemption = _matching_exemption(relative, _enclosing_function(tree, node.lineno))
                if exemption is not None:
                    honoured.add(exemption)
                    continue
                offenders.append(f"{package}: {relative}:{node.lineno} .{node.func.attr}(...)")
    assert not offenders, (
        "a module outside tripl_cli/api calls a client directly instead of sending a shared "
        f"ApiRequest: {sorted(offenders)}"
    )
    # An exemption nobody needs is a hole nobody is watching.
    assert honoured == TRANSPORT_EXEMPTIONS, (
        "a transport exemption in this test no longer matches any call; delete it: "
        f"{sorted(TRANSPORT_EXEMPTIONS - honoured)}"
    )


def test_nothing_outside_the_shared_layer_reads_a_shared_response_fact() -> None:
    """The third door, and the one tripl-ey6j.5 left open.

    Its two tests close request BUILDING: no module outside ``tripl_cli/api``
    spells a path or constructs an ``ApiRequest``. Nothing covered the other
    direction - what the response MEANS - so the same envelope was unwrapped by
    hand at four call sites across the two distributions, and they had already
    drifted apart on the routes they share.

    Scanned in BOTH packages, for the same reason the transport rule is: the
    request layer is shared, and "the shared layer answers this" enforced on one
    side only is not a rule. A projection is NOT covered here and must not be -
    ``EVENT_LIST_FIELDS`` and friends are statements about a model's context
    budget, which is a cost a CLI writing to a pipe does not have (tripl-i1dt).
    """
    offenders = [
        f"tripl_cli: {offender}"
        for offender in _shared_fact_reads(CLI_PACKAGE, RESPONSE_FACT_SOURCES)
    ] + [f"tripl_mcp: {offender}" for offender in _shared_fact_reads(MCP_PACKAGE, frozenset())]
    assert not offenders, (
        "a module reads a response key the shared layer already answers - call "
        "api.page_items/page_total, api.search.semantic_used, api.search.truncated "
        "or api.event_types.field_count instead of a second `.get`:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_the_shared_layer_re_exports_the_envelope_readers_rather_than_copying_them() -> None:
    """Identity, not equality-by-copy - the same rule ``TOOL_ENDPOINTS`` follows.

    ``tripl_cli.api`` is the whole import surface ``tripl_mcp`` has, and the
    ``{items, total}`` envelope is an envelope shape, so it is reachable from
    there. A re-export is safe; a second definition under the same name would be
    this repository's signature defect wearing the fix's clothes.
    """
    from tripl_cli import api, model

    assert api.page_items is model.page_items
    assert api.page_total is model.page_total


def test_the_diagnostics_package_holds_only_the_verdict_layers() -> None:
    """A package name is a claim about its contents; hold it to one.

    ``report.py``, ``render.py`` and ``model.py`` lived here while they built the
    ``scans``/``drifts``/``status``/``install`` documents, tables and snapshots -
    none of which reach a verdict - so every reader looking for "where is the
    JSON contract" was sent through a package saying the answer was about doctor.
    That was not a hypothetical cost: four other packages, ``tripl_cli.api``
    among them, had to import out of ``diagnostics`` for untyped-JSON helpers,
    which reads as the request layer depending on the doctor.

    A closed set rather than a "does it look diagnostic?" heuristic, because the
    failure mode is accretion one module at a time, and each single step always
    looks reasonable to whoever is taking it.
    """
    found = {source.stem for source in DIAGNOSTICS_PACKAGE.glob("*.py")}
    assert found == VERDICT_MODULES, (
        "tripl_cli/diagnostics no longer holds exactly the verdict layers - a module that "
        "serves a command reaching no verdict belongs at the package root next to report.py "
        f"(tripl-azhh): unexpected {sorted(found - VERDICT_MODULES)}, "
        f"missing {sorted(VERDICT_MODULES - found)}"
    )


def test_only_one_module_mints_a_json_envelope() -> None:
    """``report.py``'s promise, enforced instead of merely written down.

    Its docstring claims "if a key is not built here it does not exist", which
    is what makes "what does tripl emit" answerable by reading one file - and it
    is the reason the module was NOT split into a diagnostics half and a
    commands half when it moved (tripl-azhh). Nothing held anyone to it. Every
    document carries ``schema_version`` by rule, so a second module minting that
    key is a second document the one file does not describe, which is this
    repo's signature defect (one fact, two spellings) applied to the published
    contract itself.
    """
    minting = sorted(
        source.relative_to(CLI_PACKAGE).as_posix()
        for source in CLI_PACKAGE.rglob("*.py")
        if ENVELOPE_MINT in source.read_text(encoding="utf-8")
    )
    assert minting == ["report.py"], (
        "a module other than tripl_cli/report.py mints the schema_version key, so the CLI "
        f"emits a document that one file does not describe: {minting}"
    )


def test_the_declared_query_bounds_are_the_ones_the_routes_enforce(
    openapi_paths: dict[str, Any],
) -> None:
    """The 40-vs-200 trap, applied to every bound the read verbs check locally.

    ``tripl events list --limit 20000`` fails at parse time, which is right —
    it costs no request and the message names the ceiling instead of echoing a
    422. It is only right while the ceiling here IS the route's. A CLI that
    refuses a value the API accepts is a capability nobody can reach; one that
    forwards a value the API rejects turns a typo into "the instance rejected
    me". Both are silent, so the numbers are read out of the document rather
    than trusted (tripl-3ixs).
    """
    from tripl_cli.api import events, search, variables

    expected = {
        (events.LIST, "limit", "maximum"): events.LIMIT_MAX,
        (events.LIST, "limit", "default"): events.LIMIT_DEFAULT,
        (events.LIST, "silent_since_days", "maximum"): events.SILENT_SINCE_DAYS_MAX,
        # Not a bound the CLI enforces: `--order-by` is left off the wire when
        # unasked-for, and this is the value its help text tells the operator
        # that omission buys. A route that changed its default would leave the
        # help stating an ordering nobody gets (tripl-nhj0).
        (events.LIST, "order_by", "default"): events.ORDER_BY_DEFAULT,
        (variables.LIST, "limit", "maximum"): variables.LIMIT_MAX,
        (variables.LIST, "limit", "default"): variables.LIMIT_DEFAULT,
        (search.SEARCH, "limit", "maximum"): search.LIMIT_MAX,
        (search.SEARCH, "limit", "default"): search.LIMIT_DEFAULT,
        (search.SEARCH, "q", "maxLength"): search.QUERY_MAX_LENGTH,
    }
    wrong: list[str] = []
    for (path, parameter, key), declared in expected.items():
        operation = openapi_paths[f"{API_PREFIX}{path}"]["get"]
        actual = _query_schema(operation, parameter).get(key)
        if actual != declared:
            wrong.append(f"{path}?{parameter} {key}: tripl_cli says {declared}, API says {actual}")
    assert not wrong, "the CLI's query bounds are not the route's:\n  " + "\n  ".join(wrong)


def test_the_events_list_builder_takes_every_filter_the_route_declares(
    openapi_paths: dict[str, Any],
) -> None:
    """A filter the route offers and the builder cannot spell reaches no client.

    ``reviewed`` shipped to the route and to the frontend and to neither of the
    two distributions that share this builder, so the UI's "Mark reviewed" wrote
    a flag no shell and no agent could isolate afterwards. The builder's own
    comment already said it mirrors the route's Query constraints - that was a
    habit, and a habit does not fail CI. ``branch`` is resolved through a
    dependency but is declared like any other query parameter, so it appears in
    the document and this check covers it too (tripl-l33u.7; how the CLI turns a
    name into an id is test_branch_is_resolved_by_name_and_sent_as_an_id).

    No allowance list any more. This shipped with one, naming the two parameters
    that predated the shared layer - ``field_value`` from PR #78 and ``order_by``
    from PR #29 - and both are mirrored as of tripl-nhj0. An empty allowance list
    is an invitation to append to, so the set is deleted rather than emptied:
    every query parameter the route declares must be spellable here.
    """
    from tripl_cli.api import events

    operation = openapi_paths[f"{API_PREFIX}{events.LIST}"]["get"]
    declared = {
        parameter["name"]
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "query"
    }
    assert declared, "read no query parameters at all - the walk is broken, not the builder"
    accepted = set(inspect.signature(events.list_events).parameters)
    missing = sorted(declared - accepted)
    assert not missing, (
        "GET /events declares query parameters tripl_cli.api.events.list_events cannot send, "
        f"so neither the CLI nor tripl-mcp can filter on them: {missing}"
    )


def test_the_declared_enums_are_the_openapi_ones(
    openapi: dict[str, Any], openapi_paths: dict[str, Any]
) -> None:
    """``--status``, ``--type`` and ``--order-by`` are ``choices=``; hold them to the schema.

    A value the parser rejects is exit 2 on a filter the API supports, and the
    operator has no way to tell that from a typo. Checked against the components
    rather than against a copy, for the same reason ``DRIFT_STATUSES`` is a
    verbatim transcription: the enum is the API's, not this CLI's.

    ``order_by`` is a route-level ``Literal`` rather than a named schema, so its
    two values are read off the parameter itself - the same rule, the other
    corner of the document (tripl-nhj0).
    """
    from tripl_cli.api import events, search

    schemas = openapi["components"]["schemas"]
    assert list(events.STATUSES) == schemas["EventStatus"]["enum"], (
        "tripl_cli.api.events.STATUSES and the API's EventStatus enum disagree"
    )
    entity_type = schemas["SearchResult"]["properties"]["entity_type"]
    assert list(search.ENTITY_TYPES) == entity_type["enum"], (
        "tripl_cli.api.search.ENTITY_TYPES and the API's SearchResult.entity_type disagree"
    )
    order_by = _query_schema(openapi_paths[f"{API_PREFIX}{events.LIST}"]["get"], "order_by")
    assert list(events.ORDER_BY) == order_by.get("enum"), (
        "tripl_cli.api.events.ORDER_BY and GET /events?order_by's Literal disagree"
    )
    from tripl_cli.api import chart_annotations

    assert list(chart_annotations.SCOPE_TYPES) == schemas["ChartAnnotationScopeType"]["enum"], (
        "tripl_cli.api.chart_annotations.SCOPE_TYPES and the API's ChartAnnotationScopeType "
        "disagree"
    )


def test_the_sse_stream_is_deliberately_absent_from_the_spec(
    openapi_paths: dict[str, Any],
) -> None:
    """Pins the reason `tripl watch` polls instead of subscribing.

    events_stream.py declares the route `include_in_schema=False`, so this test
    would be unable to protect a watch that used it. If the route ever appears in
    the document, the transport decision recorded in watch/__init__.py should be
    revisited rather than left as folklore - but the decisive fact is the other
    one: replay chunk progress is committed to ScanJob.result_summary with no
    publish_project_event call, so it is invisible on that bus either way.
    """
    assert f"{API_PREFIX}/projects/{{slug}}/events/stream" not in openapi_paths
    assert not any("events/stream" in path for path in openapi_paths)


def test_health_is_deliberately_absent_from_the_spec(openapi_paths: dict[str, Any]) -> None:
    """Pins the reason /health is checked differently from everything else.

    main.py declares it `include_in_schema=False` and outside /api/v1. If it ever
    appears in the document, the special-cased unauthenticated probe in collect.py
    should be reconsidered rather than left as folklore.
    """
    assert "/health" not in openapi_paths
    assert f"{API_PREFIX}/health" not in openapi_paths


def test_every_finding_code_is_documented() -> None:
    """The finding codes are published as a stability contract; hold the docs to it.

    website/docs/run/cli.md carries a table of every code with its evidence keys,
    and an operator writing a cron check selects on those codes. A code that
    ships undocumented is a contract the user cannot rely on, and this is the
    repo's most-repeated review finding (AGENTS.md, "Practical Coding
    Guidance") - so it is a test rather than a habit.
    """
    assert DOCS_PATH.exists(), f"the CLI docs page is missing at {DOCS_PATH}"
    documented = set(re.findall(r"`([a-z_]+)`", DOCS_PATH.read_text(encoding="utf-8")))
    raised = _raised_finding_codes()
    assert raised, "the AST scan found no Finding(code=...) at all; the scan itself is broken"
    undocumented = raised - documented
    assert not undocumented, (
        "tripl doctor raises finding codes that website/docs/run/cli.md never mentions: "
        f"{sorted(undocumented)}"
    )


def test_the_documented_job_window_is_the_one_actually_requested() -> None:
    """The docs stated 40 while the code asked for 200, for exactly one review cycle.

    That number is not decoration: it is how far back "how long has this been
    broken" can see, and an operator reading 40 would misjudge every long streak.
    """
    from tripl_cli.model import JOBS_WINDOW

    text = DOCS_PATH.read_text(encoding="utf-8")
    assert f"**{JOBS_WINDOW}** jobs" in text, (
        f"cli.md does not state the real job window of {JOBS_WINDOW}"
    )


def test_every_watch_event_token_is_documented() -> None:
    """The event tokens ARE the watch contract; hold the docs to all of them.

    Same rule as the finding codes, and the same reason: an operator writing a
    `jq` selector picks an `event` value, so a token that ships undocumented is a
    promise the user cannot rely on. The finding-code regex cannot be reused -
    `[a-z_]+` does not match a dotted token - so each one is checked directly.
    """
    from tripl_cli.watch.model import EVENT_TOKENS

    assert DOCS_PATH.exists(), f"the CLI docs page is missing at {DOCS_PATH}"
    text = DOCS_PATH.read_text(encoding="utf-8")
    undocumented = [token for token in EVENT_TOKENS if f"`{token}`" not in text]
    assert not undocumented, (
        f"tripl watch emits event tokens website/docs/run/cli.md never mentions: {undocumented}"
    )


def test_the_documented_scans_and_drifts_numbers_are_the_ones_in_the_code() -> None:
    """The 40-vs-200 trap, applied to the four numbers the new verbs publish.

    Each is a promise an operator plans around: how much history ``scans jobs``
    can show, how far ``--limit`` may be raised, how much of a drift fan-out one
    run covers, and how long any of them waits. The repo already holds the docs
    to the doctor and watch windows for exactly this reason; the same rule now
    covers the command surface that shipped with tripl-ey6j.5.
    """
    from tripl_cli.api.scans import JOBS_LIMIT_MAX
    from tripl_cli.commands.scans import DEFAULT_JOBS_LIMIT
    from tripl_cli.diagnostics.collect import DEFAULT_MAX_EVENT_TYPES
    from tripl_cli.runner import REQUEST_TIMEOUT_SECONDS

    text = DOCS_PATH.read_text(encoding="utf-8")
    missing = [
        phrase
        for phrase in (
            f"How many jobs to ask for, `1`–`{JOBS_LIMIT_MAX}`, default `{DEFAULT_JOBS_LIMIT}`.",
            f"The default of **{DEFAULT_JOBS_LIMIT}** is the API's own default",
            f"Read budget for the fan-out, default `{DEFAULT_MAX_EVENT_TYPES}`",
        )
        if phrase not in text
    ]
    assert not missing, f"cli.md states numbers the code does not: {missing}"

    # Every --timeout row, not just one: six verbs carry it, and a row that
    # drifted would be the one the reader happened to open.
    timeout_rows = [line for line in text.splitlines() if line.startswith("| `--timeout SECONDS`")]
    assert timeout_rows, "cli.md documents no --timeout row at all"
    stale = [row for row in timeout_rows if f"default `{REQUEST_TIMEOUT_SECONDS}`" not in row]
    assert not stale, (
        f"cli.md rows state a --timeout default that is not {REQUEST_TIMEOUT_SECONDS}: {stale}"
    )


def _section(text: str, heading: str) -> str:
    """One ``### `tripl x y``` section, up to the next heading of any level."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else len(text)]


def test_the_documented_status_choices_are_exactly_the_ones_the_parser_accepts() -> None:
    """Both directions, because either drift misleads.

    A choice the parser accepts and the page omits is a capability nobody finds;
    a choice the page lists and the parser rejects is an exit 2 in somebody's
    cron. ``untriaged`` and ``all`` are ``drifts``' own additions to the API's
    four statuses, and ``events``' seven are the API's ``EventStatus`` verbatim —
    both precisely the kind of thing that gets added to a parser and not to a
    page.

    Scoped per SECTION rather than to "the one --status row in the file": two
    commands carry the flag now, they mean different vocabularies by it, and a
    whole-file match would have silently started checking whichever one happened
    to be first (tripl-3ixs).
    """
    from tripl_cli.api.events import STATUSES
    from tripl_cli.commands.drifts import STATUS_CHOICES

    text = DOCS_PATH.read_text(encoding="utf-8")
    for heading, choices, source in (
        ("### `tripl drifts list`", STATUS_CHOICES, "drifts.STATUS_CHOICES"),
        ("### `tripl events list`", STATUSES, "api.events.STATUSES"),
    ):
        section = _section(text, heading)
        rows = [line for line in section.splitlines() if line.startswith("| `--status STATUS`")]
        assert len(rows) == 1, f"expected one --status row under {heading}, found {len(rows)}"
        # `events list` documents its seven states in prose under the table
        # rather than crammed into a cell, so the whole section is searched.
        documented = set(re.findall(r"`([a-z_]+)`", section))
        assert set(choices) <= documented, (
            f"cli.md's {heading} section never names: {sorted(set(choices) - documented)} "
            f"(from {source})"
        )


def test_the_documented_read_limits_are_the_ones_in_the_code() -> None:
    """The 40-vs-200 trap, on the four ceilings the read verbs publish.

    Each is a promise an operator plans a paging loop around. A page that states
    a ceiling the parser refuses is an exit 2 somebody debugs against the docs;
    one that states a smaller ceiling than the parser accepts is throughput
    nobody reaches. The numbers themselves come from the OpenAPI document (see
    ``test_the_declared_query_bounds_are_the_ones_the_routes_enforce``), so this
    completes the chain: route -> tripl_cli.api -> argparse -> page.
    """
    from tripl_cli.api import events, search, variables

    text = DOCS_PATH.read_text(encoding="utf-8")
    missing = [
        phrase
        for phrase in (
            f"How many events to ask for, `1`–`{events.LIMIT_MAX}`, "
            f"default `{events.LIMIT_DEFAULT}`.",
            f"`0`–`{events.SILENT_SINCE_DAYS_MAX}`",
            f"How many variables to ask for, `1`–`{variables.LIMIT_MAX}`, "
            f"default `{variables.LIMIT_DEFAULT}`.",
            f"How many hits to ask for, `1`–`{search.LIMIT_MAX}`, "
            f"default `{search.LIMIT_DEFAULT}`.",
            f"1–{search.QUERY_MAX_LENGTH} characters",
        )
        if phrase not in text
    ]
    assert not missing, f"cli.md states read limits the code does not: {missing}"


def test_the_documented_entity_types_are_the_ones_search_accepts() -> None:
    """``--type`` is ``choices=``; a kind the page omits is one nobody filters on."""
    from tripl_cli.api.search import ENTITY_TYPES

    section = _section(DOCS_PATH.read_text(encoding="utf-8"), "### `tripl plan search`")
    documented = set(re.findall(r"`([a-z_]+)`", section))
    undocumented = set(ENTITY_TYPES) - documented
    assert not undocumented, (
        f"cli.md's plan search section never names these entity kinds: {sorted(undocumented)}"
    )


def _command_paths(parser: argparse.ArgumentParser, prefix: str = "tripl") -> list[str]:
    """Every command line the parser accepts, groups and verbs alike.

    Walks ``_SubParsersAction`` because argparse exposes no public way to ask a
    parser what it accepts. The module under test already names that class in a
    type hint (``commands/scans.py``), so the private access is the established
    idiom here rather than a new liberty.
    """
    paths: list[str] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            paths.append(f"{prefix} {name}")
            paths.extend(_command_paths(sub, f"{prefix} {name}"))
    return paths


def _leaf_parsers(
    parser: argparse.ArgumentParser, prefix: str = "tripl"
) -> dict[str, argparse.ArgumentParser]:
    """Every command line that actually RUNS something, keyed by its spelling.

    A group parser (``tripl scans``) is skipped: it carries no flags of its own
    and only prints help. What is left is the set a user types.
    """
    found: dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            children = _leaf_parsers(sub, f"{prefix} {name}")
            found.update(children or {f"{prefix} {name}": sub})
    return found


def _option(parser: argparse.ArgumentParser, flag: str) -> argparse.Action | None:
    return next((action for action in parser._actions if flag in action.option_strings), None)


def test_every_timeout_flag_is_the_same_flag() -> None:
    """One default and one range across every verb that carries ``--timeout``.

    Before tripl-3ixs the flag was written out at five ``add_parser`` calls plus
    two private ``_add_timeout`` helpers, and nothing held the seven together —
    a verb added with ``0.1, 60.0`` would have shipped a command that times out
    at a minute while the page and its six siblings say ten minutes. It is the
    repository's signature defect in miniature: one fact, spelled once per
    caller. The flag now has one definition (``commands.add_timeout``) and this
    walks the REAL parser to prove nobody went around it.

    Also pins the converse: a verb that talks to an instance and forgot the flag
    at all. There is no such verb, and a new one would be a command whose
    request deadline the operator cannot reach.
    """
    from tripl_cli.cli import build_parser
    from tripl_cli.commands import MAX_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS
    from tripl_cli.runner import REQUEST_TIMEOUT_SECONDS

    leaves = _leaf_parsers(build_parser())
    assert leaves, "walked the parser and found no commands — the walk is broken"
    # `install` and `upgrade` act on a DIRECTORY and poll /health on their own
    # `--wait` budget; they are the only two with no per-request deadline.
    local = {"tripl install", "tripl upgrade"}
    wrong: list[str] = []
    for path, parser in sorted(leaves.items()):
        option = _option(parser, "--timeout")
        if path in local:
            if option is not None:
                wrong.append(f"{path}: has --timeout but acts on a directory, not an instance")
            continue
        if option is None:
            wrong.append(f"{path}: talks to an instance but has no --timeout")
            continue
        if option.default != REQUEST_TIMEOUT_SECONDS:
            wrong.append(f"{path}: --timeout defaults to {option.default}")
        # The bounds live in the validator's closure, so they are checked by
        # exercising it rather than by reading an attribute that does not exist.
        parse = option.type
        assert callable(parse)
        for value in (MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS):
            parse(str(value))
        for value in (MIN_TIMEOUT_SECONDS / 2, MAX_TIMEOUT_SECONDS * 2):
            with pytest.raises(argparse.ArgumentTypeError):
                parse(str(value))
    assert not wrong, "verbs disagree about --timeout:\n  " + "\n  ".join(wrong)


def test_every_json_flag_writes_the_document_to_stdout() -> None:
    """``--json`` means the same thing everywhere, and says so in the same words.

    ``tripl watch`` is the one exception and is exempted BY NAME rather than by
    a heuristic: it emits JSON Lines, one object per event, so a help string
    promising "one JSON document" would be wrong there. Every other verb that
    reaches an instance emits exactly one document, and an operator reading
    ``tripl <anything> --help`` should not have to check whether this one is the
    odd one.
    """
    from tripl_cli.cli import build_parser

    leaves = _leaf_parsers(build_parser())
    expected = "print one JSON document on stdout and every human line on stderr"
    wrong = []
    for path, parser in sorted(leaves.items()):
        option = _option(parser, "--json")
        if option is None:
            wrong.append(f"{path}: has no --json at all")
        elif path == "tripl watch":
            if option.help == expected:
                wrong.append(f"{path}: emits JSON Lines but claims one document")
        elif option.help != expected:
            wrong.append(f"{path}: --json help is {option.help!r}")
    assert not wrong, "verbs disagree about --json:\n  " + "\n  ".join(wrong)


def test_every_command_and_verb_has_its_own_section() -> None:
    """Derived from the real parser, so a new verb fails until it is written up.

    tripl-ey6j.5 shipped six verbs with no page at all — the docs rule
    (AGENTS.md, "Practical Coding Guidance") is a habit, and a habit does not
    fail CI. A literal list of expected headings would not help: it would be
    edited by the same person who
    forgot the docs. Walking ``build_parser`` means the only way to add a
    command is to add its section.
    """
    from tripl_cli.cli import build_parser

    headings = {
        line.split("`")[1]
        for line in DOCS_PATH.read_text(encoding="utf-8").splitlines()
        if line.startswith(("## `tripl ", "### `tripl "))
    }
    expected = _command_paths(build_parser())
    assert expected, "walked the parser and found no commands — the walk is broken, not the docs"
    missing = [path for path in expected if path not in headings]
    assert not missing, f"cli.md has no section for: {missing}"


def test_the_write_safety_section_agrees_with_how_many_mutations_there_are() -> None:
    """The count of writes, spelled in prose, derived instead of typed.

    Shipping ``drifts reopen`` took the instance from three mutating verbs to
    four, and the page states "how many" in three separate places. Two went
    stale in that change and a reviewer caught them (Copilot, PR #79) — "Three
    verbs do not" sitting directly above a table listing four, and a heading
    promising ``--dry-run`` "on all three". A third, in the exit-code table,
    named two of the three prompting writes and no reviewer caught it.

    That is this repository's signature defect wearing a different hat: one
    fact, spelled once per place it is mentioned. So the number is computed here
    from what actually posts, and every sentence stating it is rebuilt from that.

    The set is derived TWICE, from independent sources, and the two must agree:
    the endpoint maps (what the CLI POSTs) and the real parser (a verb carrying
    both ``--dry-run`` and ``--project``). Either alone could go stale against
    the code — a map entry for a verb nobody wired, or a verb wired with no
    entry — and agreeing is what makes the count trustworthy enough to assert
    prose against. ``install`` and ``upgrade`` carry ``--dry-run`` too and are
    correctly excluded by both: they act on a directory and the local Docker
    daemon, never on an instance, which is why they are absent from this table.
    """
    from tripl_cli.cli import build_parser

    from_maps = {
        f"tripl {group} {key}"
        for group, mapping in (("scans", SCANS_ENDPOINTS), ("drifts", DRIFTS_ENDPOINTS))
        for key, entries in mapping.items()
        if any(method == "post" for method, _ in entries)
    } | {
        # `annotate` is a top-level verb, not a group member, so its key IS the
        # command line rather than the second half of one.
        f"tripl {key}"
        for key, entries in ANNOTATE_ENDPOINTS.items()
        if any(method == "post" for method, _ in entries)
    }
    leaves = _leaf_parsers(build_parser())
    from_parser = {
        path
        for path, parser in leaves.items()
        if _option(parser, "--dry-run") is not None and _option(parser, "--project") is not None
    }
    assert from_maps == from_parser, (
        "the endpoint maps and the parser disagree about which verbs write:\n"
        f"  only in the maps:   {sorted(from_maps - from_parser)}\n"
        f"  only in the parser: {sorted(from_parser - from_maps)}"
    )
    assert from_maps, "derived no mutating verbs at all — the derivation is broken, not the docs"

    text = DOCS_PATH.read_text(encoding="utf-8")
    start = text.index("## Write safety")
    end = text.index("\n## ", start + 1)
    section = " ".join(text[start:end].split())

    # Every mutation has a row, and the table has a row for nothing else: a
    # command listed here but not derivable is a promise the CLI does not keep.
    rows = {
        line.split("|")[1].strip().strip("`")
        for line in text[start:end].splitlines()
        if line.startswith("| `tripl ")
    }
    assert rows == from_maps, (
        f"the write-safety table lists {sorted(rows)}, but the mutations are {sorted(from_maps)}"
    )

    counts = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}
    word = counts.get(len(from_maps), str(len(from_maps)))
    claim = f"**{word} verbs do not**"
    assert claim in section, f"the write-safety intro does not say {claim!r}"
    heading = f"### `--dry-run` is on all {word.lower()}"
    assert heading in text, f"cli.md has no heading {heading!r}"

    # The exit-code table describes the writes in THREE separate clauses, and the
    # first pass at this test pinned only the one that had already been fixed —
    # so the other two shipped stale and a reviewer found them (Copilot, PR #79,
    # second pass). Naming all three is the point: this table is where an
    # operator checks what a verb's exit code means, and a mutation missing from
    # a row reads as "that case does not apply to me".
    #
    # Each is asserted as a SET read back out of the sentence, not as a rendered
    # string: the order the verbs are listed in is a null change, and a test that
    # fails on a reordering is a test people learn to edit rather than read.
    verbs = {path.removeprefix("tripl ") for path in from_maps}
    prompting = {
        path.removeprefix("tripl ")
        for path in from_maps
        if _option(leaves[path], "--yes") is not None
    }
    flat = " ".join(text.split())

    def named_before(marker: str, opener: str) -> set[str]:
        """The backticked verbs in the clause ending at ``marker``.

        Bounded by the clause's own opening rather than a character count: the
        exit-1 sentence names ``scans run`` a few words earlier for an unrelated
        reason, and a fixed-width lookback swallowed it and read it as a
        prompting write.
        """
        assert marker in flat, f"the exit-code table no longer says {marker!r}"
        head = flat[: flat.index(marker)]
        # One word or two: `annotate` is a top-level write, `scans run` a grouped one.
        return set(re.findall(r"`([a-z]+(?: [a-z]+)?)`", head[head.rindex(opener) :]))

    for label, marker, opener, expected in (
        ("accepted the write (exit 0)", ": the API accepted the write", ". ", verbs),
        (
            "declined at the prompt (exit 1)",
            " you **declined at the prompt**",
            ", and a ",
            prompting,
        ),
        (
            "non-TTY without --yes (exit 2)",
            " on a non-TTY without `--yes`**",
            " and **",
            prompting,
        ),
    ):
        found = named_before(marker, opener)
        assert found == expected, (
            f"the exit-code table's {label} clause names {sorted(found)}, "
            f"but it should name {sorted(expected)}"
        )


def test_the_page_s_closed_claim_about_dismiss_actions_is_still_true() -> None:
    """The other half of the drift-action contract, the half that faces a reader.

    ``test_drifts_cmd.py::test_no_reachable_dismiss_invocation_can_send_accept``
    already pins the constant to what goes on the wire. Nothing pinned it to the
    page, so widening ``CLI_ALLOWED_DRIFT_ACTIONS`` would have shipped a
    reachable action nobody could discover — and for a command that writes, an
    undiscoverable action is the one an operator reaches for by guessing.

    Asserting merely that each allowed action *appears* in the section would be
    no test at all: the section names ``accept`` too, precisely to say it is
    unavailable, so a substring check passes for an action the page documents as
    impossible. What the page actually makes is a CLOSED claim — "the only two
    actions reachable are X and Y" — with the complement named on the other side.
    Both sentences are rebuilt from the constants here, so moving an action
    between the tuples breaks whichever sentence went stale.

    The two sentences read DIFFERENT constants, and that is the point since
    ``reopen`` became a verb (tripl-k8j9). What ``dismiss`` can send is
    ``DISMISS_ACTIONS``; what the CLI never sends at all is the complement of
    ``CLI_ALLOWED_DRIFT_ACTIONS``, which is ``accept`` alone. Reading one
    constant for both would have the page deny that this CLI can reopen a drift,
    on the very page documenting the verb that does it.
    """
    from tripl_cli.api.event_types import (
        CLI_ALLOWED_DRIFT_ACTIONS,
        DISMISS_ACTIONS,
        DRIFT_ACTIONS,
    )

    text = DOCS_PATH.read_text(encoding="utf-8")
    start = text.index("### `tripl drifts dismiss`")
    # Bounded by the next `###`, not the next `##`: `reopen` is a sibling section
    # now, so stopping at the group boundary would swallow it and let its prose
    # satisfy a claim made about dismiss.
    #
    # Whitespace-flattened: both sentences are prose the page wraps at 80
    # columns, and one of them already straddles a line break. Matching the raw
    # text would fail on a rewrap, which is not a change in what the page claims.
    section = " ".join(text[start : text.index("\n### ", start + 1)].split())

    # Sorted, not tuple order: the constants have no production consumer that
    # reads them positionally, so reordering one is a null change and must not
    # fail CI. Membership is the whole contract.
    counts = {1: "one", 2: "two", 3: "three", 4: "four"}
    reachable = " and ".join(f"`{action}`" for action in sorted(DISMISS_ACTIONS))
    count = counts.get(len(DISMISS_ACTIONS), str(len(DISMISS_ACTIONS)))
    claim = f"the only {count} actions reachable are {reachable}"
    assert claim in section, f"cli.md's dismiss section does not claim: {claim!r}"

    withheld = sorted(set(DRIFT_ACTIONS) - set(CLI_ALLOWED_DRIFT_ACTIONS))
    assert withheld, "nothing is withheld any more — this test and the page it guards are stale"
    # `never sends X`, not `X have no spelling here`: the complement is a single
    # item now that `reopen` ships, and a sentence whose verb must agree with a
    # count the constants decide is a sentence that breaks on a null change.
    complement = " and ".join(f"`{action}`" for action in withheld)
    denial = f"never sends {complement}"
    assert denial in section, f"cli.md's dismiss section does not deny: {denial!r}"


def test_the_packaged_compose_matches_the_repo_compose() -> None:
    """The generated stack IS the production stack, minus one documented block.

    `tripl install` copies a packaged asset rather than templating YAML, so the
    only way that asset can drift from the stack this repository actually
    deploys is if somebody edits one and not the other. This is that check, and
    the fix when it fails is to re-copy - which is exactly the review
    conversation we want (tripl-ey6j.3).

    The one transform: the `mcp` service's `build:` block is removed. A fresh
    machine has no source tree, so `context: .` would point at the install
    directory, and modern compose BUILDS when an image is absent locally - an
    operator enabling `--profile mcp` would get an incomprehensible build
    failure instead of a pull.
    """
    from tripl_cli.install.files import packaged

    expected = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert MCP_BUILD_BLOCK in expected, (
        "the mcp build block this test strips is no longer in compose.yaml verbatim; "
        "update MCP_BUILD_BLOCK and re-copy the packaged asset"
    )
    assert packaged("compose.yaml") == expected.replace(MCP_BUILD_BLOCK, "")


def test_env_example_documents_every_variable_compose_forwards() -> None:
    """The other direction: a forwarded variable nobody documents is undiscoverable.

    The sibling test below catches a documented setting that reaches nothing.
    This catches its mirror image — a knob the stacks pass through that the
    template never names, so the only way to learn it exists is to read
    config.py. That is how REGISTRATION_MODE, the switch deciding whether
    strangers can create Editor accounts, went unmentioned in `.env.example`
    while compose forwarded it faithfully.

    Derived from compose rather than a hand-kept list, so the next variable
    added to the anchor and forgotten in the template fails here.
    """
    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
    template = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    # Commented entries count as documented: the template ships most optional
    # settings commented out so `cp .env.example .env` does not set them.
    documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", template, re.MULTILINE))
    forwarded = _forwarded_variables(compose)
    assert forwarded, "no forwarded variables found; the anchor shape must have changed"

    missing = sorted(forwarded - documented)
    assert not missing, (
        f"compose.yaml forwards {missing} but .env.example never names them, so an "
        "operator cannot discover the setting without reading config.py"
    )


def _forwarded_variables(compose: str) -> set[str]:
    """The names a compose file's top-level `x-*-environment` anchor passes through.

    Two-space indentation is the discriminator: a service's own `environment:`
    map sits four levels deeper, and every other key at this depth (services,
    volumes) is lower-case.
    """
    return set(re.findall(r"^  ([A-Z][A-Z0-9_]*):", compose, re.MULTILINE))


def test_both_stacks_forward_every_documented_embedding_variable() -> None:
    """A documented variable that no stack forwards is a control that does nothing.

    Compose passes an explicit allowlist - no `env_file:`, no .env mount - so a
    name in .env.example that the anchor omits reaches nothing in the container
    and the application default silently wins. That is how DEMO_ENABLED
    (tripl-2su6.16) and REGISTRATION_MODE (tripl-jfm3.101) shipped inert, and
    the third time it cost more than a switch: SEARCH_EMBEDDING_BASE_URL is the
    whole of the documented "keep indexed text on your own endpoint" control, so
    its omission POSTed tracking-plan text to api.openai.com on stacks
    configured for a self-hosted endpoint (tripl-l33u.2).

    Derived from .env.example rather than a hand-kept list, so a future
    SEARCH_EMBEDDING_* documented there and forwarded nowhere fails here.
    """
    from tripl_cli.install.files import packaged

    documented = {
        line.split("=", 1)[0]
        for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.startswith("SEARCH_EMBEDDING")
    }
    assert "SEARCH_EMBEDDING_BASE_URL" in documented, (
        ".env.example no longer documents SEARCH_EMBEDDING_BASE_URL; if the setting is "
        "gone this test is stale, and if it merely moved the privacy control is undocumented"
    )

    stacks = {
        "compose.yaml": (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"),
        "compose.dev.yaml": (REPO_ROOT / "compose.dev.yaml").read_text(encoding="utf-8"),
        "the packaged compose.yaml": packaged("compose.yaml"),
    }
    for name, compose in stacks.items():
        missing = sorted(documented - _forwarded_variables(compose))
        assert not missing, (
            f"{name} does not forward {missing}; .env.example documents them, so a value "
            "set there would never reach the container"
        )


def test_the_packaged_rabbitmq_conf_matches_the_repo_one() -> None:
    """No transform at all here - and it is not optional.

    compose.yaml bind-mounts `./infra/rabbitmq/rabbitmq.conf`. Docker's response
    to a missing bind-mount source is to create a DIRECTORY at that path, after
    which RabbitMQ fails to start with an error naming neither tripl nor the
    mount. Its `consumer_timeout` is also what stops a long metrics replay from
    being force-requeued mid-run.
    """
    from tripl_cli.install.files import packaged

    source = REPO_ROOT / "infra" / "rabbitmq" / "rabbitmq.conf"
    assert packaged("rabbitmq.conf") == source.read_text(encoding="utf-8")


def test_every_generated_variable_name_is_documented() -> None:
    """The .env `tripl install` writes is a published interface; pin the NAMES.

    A test cannot parse a shell snippet, so anti-drift for the secret RECIPES
    comes from having one executable definition (install/secrets.py) plus the
    property tests in test_install_files.py. What a test CAN pin is that every
    name that definition emits is documented in both places an operator looks -
    the CLI reference and the deployment guide - and that the deployment guide
    points at the command rather than republishing a third `openssl` recipe
    (tripl-ey6j.3).
    """
    from tripl_cli.install.secrets import REQUIRED_SECRETS, REQUIRED_SETTINGS

    pages = {
        "run/cli.md": DOCS_PATH.read_text(encoding="utf-8"),
        "run/deployment.md": DEPLOYMENT_DOCS_PATH.read_text(encoding="utf-8"),
    }
    undocumented = [
        f"{page}: {name}"
        for page, text in pages.items()
        for name in (*REQUIRED_SECRETS, *REQUIRED_SETTINGS)
        if name not in text
    ]
    assert not undocumented, (
        "`tripl install` generates variables the docs never name (the docs half of "
        "tripl-ey6j.3 lands the `## tripl install` / `## tripl upgrade` sections in "
        "run/cli.md and rewrites run/deployment.md's three hand-run procedures into a "
        "pointer): " + ", ".join(undocumented)
    )
    assert "tripl install" in pages["run/deployment.md"], (
        "website/docs/run/deployment.md never mentions `tripl install`, so an operator "
        "reading it will still hand-generate secrets from a recipe nothing tests"
    )


def test_the_documented_watch_job_window_is_the_one_actually_requested() -> None:
    """The 40-vs-200 trap again, on the number that bounds watch's load.

    watch asks for a deliberately SMALLER window than doctor because it repeats;
    a docs page that stated doctor's 200 would misrepresent both the load and how
    many jobs a single poll can notice.
    """
    from tripl_cli.watch.model import WATCH_JOBS_LIMIT

    text = DOCS_PATH.read_text(encoding="utf-8")
    assert f"**{WATCH_JOBS_LIMIT}** jobs" in text, (
        f"cli.md does not state the real watch job window of {WATCH_JOBS_LIMIT}"
    )
