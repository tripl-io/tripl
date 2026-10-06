"""Pure rules behind ``POST /projects/{slug}/plan/validate`` (GH #261, F08).

``tripl check`` scans source code for tracking calls, or reads a file of
captured payloads, and asks the plan one question per call: is this an event
the plan knows, and do its values fit? Everything that answers that question
without a database lives here, so each rule is unit-testable on plain data and
the service (``services.plan_validation_service``) only loads the plan and
hands it over.

Two ideas carry the module:

* **An identity is a string with holes.** The plan names events by a governing
  ``event_name_format`` such as ``{category}:{action}:{label}``; a call builds
  the same string from its field values. A value the scanner could not read
  (a variable, a function result) arrives as ``null`` and becomes a ``${key}``
  hole, the same token grammar the plan already uses for variables
  (``core.name_template.VARIABLE_TOKEN_PATTERN``). An interpolated name such as
  ``promo_sheet_${id}_shown`` arrives with its holes already in place. Matching
  is then "does one string with holes fit another", in both directions: the
  plan's own ``${variable}`` holes capture the call's literal text (and that
  text is then checked against the variable's allowed values), and the call's
  holes accept whatever the plan wrote there.
* **Unknown is never wrong.** A hole is not an error. A value the scanner
  could not read produces no finding unless the caller asks for ``strict``,
  and then only an ``info`` note. An identity with holes that matches nothing
  is reported as a WARNING, not an error: the scanner cannot prove the call
  sends an unplanned event, only that nothing it could read matches one.

Contract checks mirror the warehouse contracts the schema-drift job runs
(``worker.tasks.metrics.schema_drift``): ``enum_options`` for an enum field,
``contract_regex`` as a PARTIAL match (``re.search``, which is what
``REGEXP_CONTAINS``, ClickHouse ``match()`` and the fallback adapter do), and
``contract_min_value`` / ``contract_max_value`` with a non-numeric value
counting as out of range, as the fallback adapter counts it.
"""

from __future__ import annotations

import functools
import itertools
import math
import re
import uuid
from bisect import bisect_left
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from tripl.core.event_properties import EventProperty, event_properties, kind_of, type_accepts
from tripl.core.name_template import (
    NAME_FORMAT_PATTERN,
    VARIABLE_TOKEN_PATTERN,
    format_keys,
    resolve_dotted_keys,
)
from tripl.json_paths import format_json_path_value

Severity = Literal["error", "warning", "info"]
ItemStatus = Literal["ok", "warning", "error"]

CODE_UNKNOWN_EVENT_TYPE: Final = "unknown_event_type"
CODE_UNKNOWN_EVENT: Final = "unknown_event"
CODE_DEPRECATED_EVENT: Final = "deprecated_event"
CODE_UNKNOWN_FIELD: Final = "unknown_field"
CODE_MISSING_REQUIRED_FIELD: Final = "missing_required_field"
CODE_VALUE_NOT_ALLOWED: Final = "value_not_allowed"
CODE_DYNAMIC_VALUE: Final = "dynamic_value"
CODE_TOO_DYNAMIC: Final = "too_dynamic"
CODE_WRONG_TYPE: Final = "wrong_type"
CODE_POLICY_VIOLATION: Final = "policy_violation"

#: The most ``${...}`` holes one query identity may carry. Every hole is a lazy
#: ``(.*?)`` group, and a pattern with many of them backtracks badly against a
#: long identity; a call that dynamic identifies next to nothing anyway.
MAX_HOLES_PER_QUERY: Final = 10

FindingCode = Literal[
    "unknown_event_type",
    "unknown_event",
    "deprecated_event",
    "unknown_field",
    "missing_required_field",
    "value_not_allowed",
    "dynamic_value",
    "too_dynamic",
    "wrong_type",
    "policy_violation",
]

STATUS_DEPRECATED: Final = "deprecated"
STATUS_ARCHIVED: Final = "archived"

# What a caller's hole becomes when the plan's own template has to be matched
# against the call's identity: a character no stored name can contain (Postgres
# ``text`` cannot hold U+0000), so a plan literal can never match it and a plan
# ``${variable}`` capture containing it is known to be partly dynamic.
_HOLE: Final = "\x00"


@dataclass(frozen=True)
class Finding:
    code: FindingCode
    severity: Severity
    field: str | None
    message: str
    # The extension rule a ``policy_violation`` finding reports (``core.plan_policy``).
    rule: str | None = None


@dataclass(frozen=True)
class PlanField:
    name: str
    field_type: str
    is_required: bool = False
    enum_options: tuple[str, ...] = ()
    regex: str | None = None
    min_value: float | None = None
    max_value: float | None = None


@dataclass(frozen=True)
class PlanEventType:
    id: uuid.UUID
    name: str
    name_format: str | None
    fields: Mapping[str, PlanField]


@dataclass(frozen=True)
class PlanEvent:
    id: uuid.UUID
    event_type_id: uuid.UUID
    name: str
    identity: str
    status: str


@dataclass(frozen=True)
class IdentityMatch:
    event: PlanEvent
    # ``${variable}`` holes of the plan identity -> the call's literal text there.
    # A capture the call itself left dynamic is omitted.
    captures: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ValidationItem:
    ref: str
    event_type: str | None = None
    name: str | None = None
    fields: Mapping[str, str | None] = field(default_factory=dict)
    properties: Mapping[str, object] | None = None
    complete: bool = False


@dataclass(frozen=True)
class EventContext:
    """What the checks need about ONE matched event, loaded after resolution."""

    # Field name -> the value the plan stores for this event (may hold ``${var}``).
    field_values: Mapping[str, str] = field(default_factory=dict)
    # Variable token -> this event's override list, which REPLACES the global one.
    overrides: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Tokens of the variables this event's property list marks required (F23).
    required_tokens: frozenset[str] = frozenset()


@dataclass
class Resolution:
    item: ValidationItem
    event_type: PlanEventType | None = None
    identity: str | None = None
    event: PlanEvent | None = None
    captures: tuple[tuple[str, str], ...] = ()
    findings: list[Finding] = field(default_factory=list)
    # True when a finding already makes further checks meaningless.
    stopped: bool = False


# ---------------------------------------------------------------------------
# Holes and templates
# ---------------------------------------------------------------------------


def has_holes(value: str | None) -> bool:
    """``None`` (unreadable) or a value containing a ``${...}`` token."""
    return value is None or VARIABLE_TOKEN_PATTERN.search(value) is not None


def has_literal_content(identity: str) -> bool:
    """Whether anything but holes and separators is known about ``identity``.

    ``${category}:${action}:${label}`` matches every event of the type, which
    identifies nothing; such a call is left unresolved rather than matched.
    Unicode-aware: ``экран:${action}`` is as identifiable as ``screen:${action}``.
    """
    return any(ch.isalnum() for ch in VARIABLE_TOKEN_PATTERN.sub("", identity))


def hole_count(identity: str) -> int:
    return len(VARIABLE_TOKEN_PATTERN.findall(identity))


@functools.lru_cache(maxsize=8192)
def longest_literal(template: str) -> str:
    """The longest run of literal text between the ``${...}`` holes of ``template``.

    Any identity ``template`` matches contains it verbatim, so a substring test
    is a cheap filter in front of the regex when there is no literal prefix.
    """
    return max(VARIABLE_TOKEN_PATTERN.sub(_HOLE, template).split(_HOLE), key=len)


@functools.lru_cache(maxsize=8192)
def template_regex(template: str) -> tuple[re.Pattern[str], tuple[str, ...]]:
    """``template`` as an anchored regex whose groups are its ``${...}`` holes.

    Literal text is escaped; each hole matches any text, lazily, so the groups
    come back in template order. Callers use ``fullmatch``.
    """
    names: list[str] = []
    parts: list[str] = []
    last = 0
    for match in VARIABLE_TOKEN_PATTERN.finditer(template):
        parts.append(re.escape(template[last : match.start()]))
        parts.append("(.*?)")
        names.append(match.group(1))
        last = match.end()
    parts.append(re.escape(template[last:]))
    return re.compile("".join(parts), re.DOTALL), tuple(names)


def literal_prefix(template: str) -> str:
    match = VARIABLE_TOKEN_PATTERN.search(template)
    return template if match is None else template[: match.start()]


#: What the scan writes into the event name for a key the row does not carry
#: (a NULL column, a JSON path the row lacks): ``event_plan._format_value`` and
#: the ``fmt_kwargs[key] = ""`` seed in ``event_plan``.
SCAN_MISSING_VALUE: Final = ""


def build_identity(
    name_format: str, values: Mapping[str, str | None], *, missing: str | None = None
) -> str:
    """The identity a call's values give under ``name_format``, with holes.

    Known values substitute their ``{key}``; a key whose value is ``None``
    (unreadable) becomes a ``${key}`` hole. A key the call does not name at
    all is a hole too, unless ``missing`` is given: a complete payload says
    everything the event carries, so an absent key renders the way the scan
    renders a missing value (``SCAN_MISSING_VALUE``). Dotted keys
    (``{payload.screen}``) are walked out of a literal JSON value exactly as the
    scan does (``resolve_dotted_keys``).
    """
    literal = {key: value for key, value in values.items() if value is not None}
    resolved = resolve_dotted_keys(name_format, literal)

    def _substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        known = resolved.get(key)
        if known is not None:
            return known
        if missing is not None and not _is_dynamic(key, values):
            return missing
        return f"${{{key}}}"

    return NAME_FORMAT_PATTERN.sub(_substitute, name_format)


def _is_dynamic(key: str, values: Mapping[str, str | None]) -> bool:
    """Whether ``key`` is unknown because the call could not read it, not absent."""
    if key in values:
        return values[key] is None
    base = key.split(".", 1)[0]
    if base != key and base in values:
        raw = values[base]
        return raw is None or has_holes(raw)
    return False


def _format_keys_base(name_format: str) -> set[str]:
    return {key.split(".", 1)[0] for key in format_keys(name_format)}


# ---------------------------------------------------------------------------
# Identity index
# ---------------------------------------------------------------------------


class IdentityIndex:
    """Plan events of one scope (one type, or the whole plan) by identity.

    ``match`` answers a query identity in three steps, cheapest first: exact
    identity, exact display name, then the hole-aware comparisons. Results are
    memoised per query because a scan reports the same call many times.
    """

    def __init__(self, events: Iterable[PlanEvent]) -> None:
        self._by_identity: dict[str, list[PlanEvent]] = {}
        self._by_name: dict[str, list[PlanEvent]] = {}
        self._templated: list[PlanEvent] = []
        for event in events:
            self._by_identity.setdefault(event.identity, []).append(event)
            self._by_name.setdefault(event.name, []).append(event)
            if VARIABLE_TOKEN_PATTERN.search(event.identity):
                self._templated.append(event)
        self._sorted = sorted(self._by_identity)
        self._memo: dict[str, tuple[IdentityMatch, ...]] = {}

    def match(self, query: str) -> tuple[IdentityMatch, ...]:
        cached = self._memo.get(query)
        if cached is None:
            cached = tuple(_dedupe(self._match(query)))
            self._memo[query] = cached
        return cached

    def _match(self, query: str) -> Iterable[IdentityMatch]:
        if not has_holes(query):
            exact = self._by_identity.get(query) or self._by_name.get(query)
            if exact:
                return [IdentityMatch(event) for event in exact]
            return self._match_templates(query)
        return [*self._match_query_holes(query), *self._match_templates(_fill_holes(query))]

    def _match_templates(self, subject: str) -> list[IdentityMatch]:
        """Plan identities with ``${variable}`` holes that ``subject`` fits."""
        found: list[IdentityMatch] = []
        for event in self._templated:
            if not subject.startswith(literal_prefix(event.identity)):
                continue
            if longest_literal(event.identity) not in subject:
                continue
            pattern, names = template_regex(event.identity)
            hit = pattern.fullmatch(subject)
            if hit is None:
                continue
            captures = tuple(
                (name, text)
                for name, text in zip(names, hit.groups(), strict=True)
                if _HOLE not in text
            )
            found.append(IdentityMatch(event, captures))
        return found

    def _match_query_holes(self, query: str) -> list[IdentityMatch]:
        """Plan identities (taken literally) that fit a query with holes."""
        pattern, _ = template_regex(query)
        prefix = literal_prefix(query)
        candidates: Iterable[str]
        if prefix:
            start = bisect_left(self._sorted, prefix)
            candidates = itertools.takewhile(
                lambda identity: identity.startswith(prefix), self._sorted[start:]
            )
        else:
            # No prefix to bisect on: a substring test on the longest literal
            # run keeps the regex off every identity that cannot match.
            segment = longest_literal(query)
            candidates = (identity for identity in self._sorted if segment in identity)
        found: list[IdentityMatch] = []
        for identity in candidates:
            if pattern.fullmatch(identity):
                found.extend(IdentityMatch(event) for event in self._by_identity[identity])
        return found


def _fill_holes(query: str) -> str:
    return VARIABLE_TOKEN_PATTERN.sub(_HOLE, query)


def _dedupe(matches: Iterable[IdentityMatch]) -> list[IdentityMatch]:
    seen: set[uuid.UUID] = set()
    out: list[IdentityMatch] = []
    for match in matches:
        if match.event.id in seen:
            continue
        seen.add(match.event.id)
        out.append(match)
    return out


@dataclass
class PlanSnapshot:
    """The plan of one branch, shaped for validation. Built once per request."""

    types_by_name: Mapping[str, PlanEventType]
    events: Sequence[PlanEvent]
    # Every token naming a variable (``VariableIndex.tokens_of``: name, source
    # name, bindings; first variable by name wins a token, as in the scan) ->
    # that variable's global allowed values.
    variable_allowed: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Variable id -> the tokens it WON above, so a per-event override is keyed
    # by exactly the tokens the global list is.
    variable_tokens: Mapping[uuid.UUID, tuple[str, ...]] = field(default_factory=dict)
    # Every token above -> its variable's (variable_type, json_schema) (F23).
    variable_types: Mapping[str, tuple[str, Mapping[str, Any] | None]] = field(default_factory=dict)
    by_type: dict[uuid.UUID, IdentityIndex] = field(init=False)
    everything: IdentityIndex = field(init=False)
    types_by_id: dict[uuid.UUID, PlanEventType] = field(init=False)

    def __post_init__(self) -> None:
        self.types_by_id = {et.id: et for et in self.types_by_name.values()}
        grouped: dict[uuid.UUID, list[PlanEvent]] = {et_id: [] for et_id in self.types_by_id}
        for event in self.events:
            grouped.setdefault(event.event_type_id, []).append(event)
        self.by_type = {et_id: IdentityIndex(rows) for et_id, rows in grouped.items()}
        self.everything = IdentityIndex(self.events)


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def scalar_text(value: object) -> str | None:
    """A payload value as the text a warehouse column would hold, or ``None``.

    Strings pass through, numbers and booleans render the way the scan renders
    JSON values; ``null`` and containers are not literal values.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return format_json_path_value(value)
    return None


def collect_values(item: ValidationItem) -> dict[str, str | None]:
    """Every field key the item names, ``fields`` winning over ``properties``."""
    values: dict[str, str | None] = {}
    for key, raw in (item.properties or {}).items():
        values[key] = scalar_text(raw)
    values.update(item.fields)
    return values


def check_field_contract(fd: PlanField, value: str) -> list[Finding]:
    """``enum_options`` / ``contract_regex`` / min-max for one literal value."""
    findings: list[Finding] = []
    if fd.field_type == "enum" and fd.enum_options and value not in fd.enum_options:
        allowed = ", ".join(fd.enum_options)
        findings.append(
            Finding(
                CODE_VALUE_NOT_ALLOWED,
                "error",
                fd.name,
                f"'{value}' is not one of the allowed values of '{fd.name}' ({allowed})",
            )
        )
    if fd.regex:
        pattern = _compile_contract(fd.regex)
        if pattern is not None and pattern.search(value) is None:
            findings.append(
                Finding(
                    CODE_VALUE_NOT_ALLOWED,
                    "error",
                    fd.name,
                    f"'{value}' does not match the contract pattern of '{fd.name}' ({fd.regex})",
                )
            )
    bounds = (fd.min_value, fd.max_value)
    if any(bound is not None for bound in bounds) and all(
        bound is None or math.isfinite(bound) for bound in bounds
    ):
        findings.extend(_range_findings(fd, value))
    return findings


def _range_findings(fd: PlanField, value: str) -> list[Finding]:
    try:
        number = float(value)
    except ValueError:
        return [
            Finding(
                CODE_VALUE_NOT_ALLOWED,
                "error",
                fd.name,
                f"'{value}' is not a number, and '{fd.name}' has a numeric range contract",
            )
        ]
    if not math.isfinite(number):
        return [
            Finding(CODE_VALUE_NOT_ALLOWED, "error", fd.name, f"'{value}' is not a finite number")
        ]
    if fd.min_value is not None and number < fd.min_value:
        return [
            Finding(
                CODE_VALUE_NOT_ALLOWED,
                "error",
                fd.name,
                f"{value} is below the minimum {_num(fd.min_value)} of '{fd.name}'",
            )
        ]
    if fd.max_value is not None and number > fd.max_value:
        return [
            Finding(
                CODE_VALUE_NOT_ALLOWED,
                "error",
                fd.name,
                f"{value} is above the maximum {_num(fd.max_value)} of '{fd.name}'",
            )
        ]
    return []


def _num(bound: float) -> str:
    number = float(bound)
    return str(int(number)) if number.is_integer() else str(number)


@functools.lru_cache(maxsize=1024)
def _compile_contract(regex: str) -> re.Pattern[str] | None:
    """A contract pattern, or ``None`` when Python cannot compile it.

    The warehouse dialects differ from Python's; a pattern only they accept is
    skipped here rather than reported against every value.
    """
    try:
        return re.compile(regex)
    except re.error:
        return None


def allowed_for(
    variable: str, ctx: EventContext | None, variable_allowed: Mapping[str, tuple[str, ...]]
) -> tuple[str, ...]:
    """The event's override list when it has one, else the variable's global list."""
    if ctx is not None:
        override = ctx.overrides.get(variable)
        if override:
            return override
    return variable_allowed.get(variable, ())


def check_variable_value(
    variable: str,
    value: str,
    *,
    field_name: str | None,
    ctx: EventContext | None,
    variable_allowed: Mapping[str, tuple[str, ...]],
) -> Finding | None:
    """``value`` against the allowed values of ``${variable}``; empty = unconstrained."""
    allowed = allowed_for(variable, ctx, variable_allowed)
    if not allowed or value in allowed:
        return None
    return Finding(
        CODE_VALUE_NOT_ALLOWED,
        "error",
        field_name,
        f"'{value}' is not an allowed value of variable ${{{variable}}} ({', '.join(allowed)})",
    )


def check_template_value(
    field_name: str,
    template: str,
    value: str,
    *,
    ctx: EventContext | None,
    variable_allowed: Mapping[str, tuple[str, ...]],
) -> tuple[list[Finding], set[str]]:
    """A literal value against the plan's stored ``${variable}`` template.

    Returns the findings and the variables checked. A stored literal, or a
    value that does not have the template's shape, is not judged here: stored
    values of scanned events are representative samples, not rules.
    """
    if not VARIABLE_TOKEN_PATTERN.search(template):
        return [], set()
    pattern, names = template_regex(template)
    hit = pattern.fullmatch(value)
    if hit is None:
        return [], set()
    findings: list[Finding] = []
    checked: set[str] = set()
    for name, text in zip(names, hit.groups(), strict=True):
        checked.add(name)
        finding = check_variable_value(
            name, text, field_name=field_name, ctx=ctx, variable_allowed=variable_allowed
        )
        if finding is not None:
            findings.append(finding)
    return findings, checked


# ---------------------------------------------------------------------------
# Resolution and verdicts
# ---------------------------------------------------------------------------


def resolve_item(item: ValidationItem, plan: PlanSnapshot) -> Resolution:
    """Which event type and which event the item is, before any value check.

    * ``event_type`` given: the type must exist (``unknown_event_type``). When
      the type has a naming rule and the item names any of its keys, or names
      no event at all, the identity is BUILT from the rule; otherwise the item's
      ``name`` is the identity. A built identity with nothing literal in it
      falls back to the item's ``name`` when there is one. In payload mode
      (``complete``) a rule key the item lacks renders as the scan renders a
      missing value, not as a hole. Matching stays within the type.
    * only ``name``: matched across every type.

    An identity with holes and no literal text is left unresolved; one with
    more than ``MAX_HOLES_PER_QUERY`` holes gets a ``too_dynamic`` note and is
    skipped. A fully literal identity is always looked up.
    """
    res = Resolution(item=item)
    index: IdentityIndex
    if item.event_type is not None:
        etype = plan.types_by_name.get(item.event_type)
        if etype is None:
            res.findings.append(
                Finding(
                    CODE_UNKNOWN_EVENT_TYPE,
                    "error",
                    None,
                    f"The plan has no event type '{item.event_type}'",
                )
            )
            res.stopped = True
            return res
        res.event_type = etype
        index = plan.by_type[etype.id]
        values = collect_values(item)
        if etype.name_format and (
            not item.name or _format_keys_base(etype.name_format) & values.keys()
        ):
            built = build_identity(
                etype.name_format,
                values,
                missing=SCAN_MISSING_VALUE if item.complete else None,
            )
            # The call's own literal name identifies more than a rule rendered
            # from values it could not read.
            res.identity = item.name if item.name and not has_literal_content(built) else built
        else:
            res.identity = item.name
    else:
        if not item.name:
            res.findings.append(
                Finding(
                    CODE_UNKNOWN_EVENT,
                    "error",
                    None,
                    "Nothing identifies the event: give an event type or a name",
                )
            )
            res.stopped = True
            return res
        index = plan.everything
        res.identity = item.name

    if not res.identity:
        # A type with no naming rule and no name: nothing to match.
        return res
    if has_holes(res.identity):
        if not has_literal_content(res.identity):
            # All holes and separators: nothing to match, and nothing wrong
            # with the call either. A fully literal identity is always looked up.
            return res
        if hole_count(res.identity) > MAX_HOLES_PER_QUERY:
            res.findings.append(
                Finding(
                    CODE_TOO_DYNAMIC,
                    "info",
                    None,
                    f"The event name has more than {MAX_HOLES_PER_QUERY} runtime parts "
                    f"and was not checked: '{res.identity}'",
                )
            )
            res.stopped = True
            return res

    matches = index.match(res.identity)
    if not matches:
        dynamic = has_holes(res.identity)
        scope = f" of type '{res.event_type.name}'" if res.event_type else ""
        res.findings.append(
            Finding(
                CODE_UNKNOWN_EVENT,
                "warning" if dynamic else "error",
                None,
                (
                    f"No planned event{scope} fits '{res.identity}'"
                    if dynamic
                    else f"The plan has no event{scope} '{res.identity}'"
                ),
            )
        )
        return res
    if len(matches) == 1:
        res.event = matches[0].event
        res.captures = matches[0].captures
    if res.event_type is None:
        type_ids = {m.event.event_type_id for m in matches}
        if len(type_ids) == 1:
            res.event_type = plan.types_by_id.get(next(iter(type_ids)))
    return res


def check_item(
    res: Resolution,
    plan: PlanSnapshot,
    ctx: EventContext | None,
    *,
    strict: bool = False,
) -> list[Finding]:
    """Every finding for one resolved item, resolution findings first."""
    findings = list(res.findings)
    if res.stopped:
        return findings
    item = res.item
    event = res.event
    if event is not None:
        if event.status == STATUS_ARCHIVED:
            findings.append(
                Finding(
                    CODE_DEPRECATED_EVENT,
                    "error",
                    None,
                    f"'{event.name}' is archived: it must no longer be sent",
                )
            )
        elif event.status == STATUS_DEPRECATED:
            findings.append(
                Finding(
                    CODE_DEPRECATED_EVENT,
                    "warning",
                    None,
                    f"'{event.name}' is deprecated",
                )
            )

    values = collect_values(item)
    checked_variables: set[str] = set()
    etype = res.event_type
    props = _properties_of(etype, ctx, plan) if event is not None else {}
    payload, as_text = _flat_payload(item, etype, props)
    if etype is not None:
        for key, value in values.items():
            fd = etype.fields.get(key)
            if fd is None and (key in props or any(p.startswith(f"{key}.") for p in props)):
                # A property, or the object holding nested ones (``cart`` for
                # ``cart.items``): checked below, path by path.
                continue
            if fd is None:
                findings.append(
                    Finding(
                        CODE_UNKNOWN_FIELD,
                        "warning",
                        key,
                        f"'{key}' is not a field of event type '{etype.name}'",
                    )
                )
                continue
            if has_holes(value):
                if strict:
                    findings.append(_dynamic(key))
                continue
            assert value is not None  # narrowed by has_holes
            findings.extend(check_field_contract(fd, value))
            template = ctx.field_values.get(key) if ctx is not None else None
            if template:
                found, checked = check_template_value(
                    key, template, value, ctx=ctx, variable_allowed=plan.variable_allowed
                )
                findings.extend(found)
                checked_variables |= checked
        findings.extend(
            _property_findings(
                props,
                payload,
                as_text=as_text,
                ctx=ctx,
                plan=plan,
                checked=checked_variables,
                strict=strict,
            )
        )
        if item.complete:
            for fd in etype.fields.values():
                if fd.is_required and fd.name not in values:
                    findings.append(
                        Finding(
                            CODE_MISSING_REQUIRED_FIELD,
                            "error",
                            fd.name,
                            f"Required field '{fd.name}' is missing",
                        )
                    )
        # ``properties`` sent as null (the CLI's size limit) says nothing about
        # which properties the call carried.
        if item.complete and (item.properties is not None or as_text):
            for path, prop in props.items():
                if prop.required and path not in payload:
                    findings.append(
                        Finding(
                            CODE_MISSING_REQUIRED_FIELD,
                            "error",
                            path,
                            f"Required property '{path}' is missing",
                        )
                    )
    elif strict:
        findings.extend(_dynamic(key) for key, value in values.items() if has_holes(value))

    if event is not None:
        for variable, text in res.captures:
            if variable in checked_variables:
                continue
            checked_variables.add(variable)
            finding = check_variable_value(
                variable, text, field_name=None, ctx=ctx, variable_allowed=plan.variable_allowed
            )
            if finding is not None:
                findings.append(finding)
    if strict and res.identity is not None and has_holes(res.identity):
        findings.append(
            Finding(
                CODE_DYNAMIC_VALUE,
                "info",
                None,
                f"The event name is only partly known at scan time: '{res.identity}'",
            )
        )
    return findings


def _properties_of(
    etype: PlanEventType | None, ctx: EventContext | None, plan: PlanSnapshot
) -> dict[str, EventProperty]:
    """The matched event's typed JSON properties by path; first field wins a path."""
    if etype is None or ctx is None:
        return {}
    by_path: dict[str, EventProperty] = {}
    for prop in event_properties(
        ctx.field_values,
        [fd.name for fd in etype.fields.values() if fd.field_type == "json"],
        token_types=plan.variable_types,
        required_tokens=ctx.required_tokens,
        allowed_for=lambda token: allowed_for(token, ctx, plan.variable_allowed),
    ):
        by_path.setdefault(prop.path, prop)
    return by_path


def _flat_payload(
    item: ValidationItem, etype: PlanEventType | None, props: Mapping[str, EventProperty]
) -> tuple[dict[str, object], set[str]]:
    """The payload's property values by dotted path, and the paths read as text.

    A nested object is walked (``{"cart": {"total": 1}}`` -> ``cart.total``)
    until it reaches a property's own path, whose value is kept whole: a
    ``json`` property holds an object. One under a JSON FIELD's own name is
    that field's content, so the field name is not part of the path: a payload
    may send ``{"properties": {...}}`` or the properties at the top level.
    A key of ``fields`` that is a property, not a field, counts too; its value
    is warehouse text, so its type cannot be judged.
    """
    json_fields = (
        {fd.name for fd in etype.fields.values() if fd.field_type == "json"} if etype else set()
    )
    flat: dict[str, object] = {}

    def walk(prefix: str, value: object) -> None:
        if isinstance(value, dict) and prefix not in props:
            for key, nested in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), nested)
        elif prefix:
            flat[prefix] = value

    for key, raw in (item.properties or {}).items():
        walk("" if key in json_fields and isinstance(raw, dict) else key, raw)
    as_text: set[str] = set()
    fields = etype.fields if etype is not None else {}
    for key, text in item.fields.items():
        if key not in fields and key in props and key not in flat:
            flat[key] = text
            as_text.add(key)
    return flat, as_text


def _property_findings(
    props: Mapping[str, EventProperty],
    payload: Mapping[str, object],
    *,
    as_text: Collection[str],
    ctx: EventContext | None,
    plan: PlanSnapshot,
    checked: set[str],
    strict: bool,
) -> list[Finding]:
    """Each payload value that is a property, against its type and allowed values."""
    findings: list[Finding] = []
    for path, raw in payload.items():
        prop = props.get(path)
        if prop is None:
            continue
        text = scalar_text(raw)
        if raw is None or (text is not None and has_holes(text)):
            if strict:
                findings.append(_dynamic(path))
            continue
        if prop.template is not None and text is not None:
            found, variables = check_template_value(
                path, prop.template, text, ctx=ctx, variable_allowed=plan.variable_allowed
            )
            findings.extend(found)
            checked |= variables
            continue
        if prop.token is None:
            # A stored literal is a sample of what a scanned event sent, not a
            # rule (``check_template_value`` says the same of fields).
            continue
        if path not in as_text and prop.variable_type and not type_accepts(prop.variable_type, raw):
            findings.append(
                Finding(
                    CODE_WRONG_TYPE,
                    "error",
                    path,
                    f"'{path}' is {kind_of(raw)}, but the plan types it as {prop.variable_type}",
                )
            )
            continue
        if text is None:
            continue
        checked.add(prop.token)
        finding = check_variable_value(
            prop.token, text, field_name=path, ctx=ctx, variable_allowed=plan.variable_allowed
        )
        if finding is not None:
            findings.append(finding)
    return findings


def _dynamic(key: str) -> Finding:
    return Finding(
        CODE_DYNAMIC_VALUE, "info", key, f"'{key}' is set at runtime and was not checked"
    )


def item_status(findings: Sequence[Finding]) -> ItemStatus:
    severities = {finding.severity for finding in findings}
    if "error" in severities:
        return "error"
    if "warning" in severities:
        return "warning"
    return "ok"
