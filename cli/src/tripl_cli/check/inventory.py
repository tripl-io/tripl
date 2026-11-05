"""What ``tripl check --list`` shows: the scan's two passes, with what they saw.

``tripl check`` sends what ``scan()`` maps and lets the rest go, and writing a
check config needs that rest. For every call the config matches: what was read
for each target and, for a value that was not, why (``model.REASON_CODES``:
the evaluator's reason, refined here from the function the call sits in). Then
the functions that forward their own parameter into a tracking call, whose
callers hold the value and want a call spec of their own; and the calls that
look like tracking but match no call spec.

``inventory()`` runs ``scan``'s own passes, so what it lists is what ``tripl
check`` reads; and nothing here flows back into ``scan()``, so what ``tripl
check`` sends or decides never depends on it. A config with no event types is
fine here (``scan()`` refuses one): nothing matches, and the calls that look
like tracking are what is left to show.

Nothing here touches the network.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tripl_cli.check.calls import Call
from tripl_cli.check.config import (
    FIELD_PREFIX,
    NAME,
    PROPERTIES,
    RESERVED_TARGETS,
    CallSpec,
    CheckConfig,
    EventTypeSpec,
    default_target,
    selector_field,
)
from tripl_cli.check.model import (
    REASON_LOCAL,
    REASON_NOT_PASSED,
    REASON_PARAMETER,
    REASON_PARAMETER_PROPERTY,
    REASON_UNKNOWN_CONSTANT,
    REASON_VARIABLE,
    REASON_WRONG_SHAPE,
    SIGNAL_CONSTANT,
    SIGNAL_NAME,
    SIGNAL_RECEIVER,
    SIGNALS,
    CheckItem,
    Origin,
    SkippedFile,
)
from tripl_cli.check.scan import (
    FileCalls,
    call_items,
    call_origin,
    collect_call,
    discover,
    first_pass,
    second_pass,
)
from tripl_cli.check.scopes import FunctionScope, ScopeIndex
from tripl_cli.check.signatures import Parameter
from tripl_cli.check.values import Evaluator, Value, clip, pure_path

# With no check config `--list` scans a whole repository, and stops after this
# many source files (a home directory that is itself a checkout, a monorepo):
# the first pass keeps every file's text and tokens.
MAX_DISCOVERY_FILES = 20_000
# The longest example of a call that looks like tracking; a longer one is cut.
EXAMPLE_MAX = 80


# --- the data -----------------------------------------------------------------------
@dataclass(frozen=True)
class Location:
    """A place in a scanned file: its path under the root, a 1-based line and column."""

    path: str
    line: int
    column: int | None = None


@dataclass(frozen=True)
class Declared:
    """Where a value the scan refined comes from: a function, and a line of its file.

    ``line`` is the function's declaration line for ``parameter`` and
    ``parameter_property`` (``parameter`` says which one), the local's own
    line for ``local``.
    """

    function: str
    line: int
    parameter: Parameter | None = None


@dataclass(frozen=True)
class ReadValue:
    """What the scan read for one target of a call, and why when it could not.

    ``reason`` is the value's own (``Value.reason``) unless the scan refined a
    ``variable`` from the function the call sits in; empty for a value read.
    """

    value: Value
    reason: str = ""
    declared: Declared | None = None


@dataclass(frozen=True)
class Read:
    """A matched call's arguments, per target its call spec maps.

    Top-level values only, the ones an object argument assigns included: the
    entries of a dictionary keep the evaluator's own reasons.
    """

    name: ReadValue | None = None
    fields: Mapping[str, ReadValue] = field(default_factory=dict)
    properties: ReadValue | None = None
    # The labels, or `#index`, of the arguments the spec maps to no target.
    unmapped: tuple[str, ...] = ()
    # The object argument, when it is no literal the scan reads (a variable, a
    # call): then none of its keys reached a target.
    unread_object: Value | None = None

    def targets(self) -> Iterator[tuple[str, ReadValue]]:
        """``(target, value)`` for the name, each field, then properties.

        A field named like a reserved target is ``field:<name>``, as a call
        spec addresses it, so ``field:name`` is never the event's name.
        """
        if self.name is not None:
            yield NAME, self.name
        for name, value in self.fields.items():
            yield _field_target(name), value
        if self.properties is not None:
            yield PROPERTIES, self.properties


def _field_target(name: str) -> str:
    return FIELD_PREFIX + name if name in RESERVED_TARGETS else name


@dataclass(frozen=True)
class ListedCall:
    """One call site the check config matches, as ``tripl check --list`` shows it."""

    origin: Origin
    event_type: str
    read: Read
    # Every target the items carry as dynamic, keyed as `Read.targets` keys it,
    # with exactly one reason: its value's, else `not_passed` (the call passes
    # none), else `wrong_shape` (a value of the wrong kind: properties as text,
    # a name as a dictionary).
    dynamic: Mapping[str, str]
    # `(field, label)`: a field a labelled argument reached through the default
    # label mapping, because the call spec has no `args:`.
    default_label_fields: tuple[tuple[str, str], ...]
    # What `tripl check` builds for this call, exactly as `scan()` does. The
    # validator's size limits are applied later (`payloads.enforce_limits`).
    items: tuple[CheckItem, ...]

    @property
    def fully_read(self) -> bool:
        return not any(item.dynamic for item in self.items)

    @property
    def sent(self) -> bool:
        """False when nothing about the call is known: it is reported, never sent."""
        return any(item.sendable for item in self.items)


@dataclass(frozen=True)
class UnmatchedGroup:
    """Calls that look like tracking but match no call spec, grouped by method.

    ``method`` is the last callee segment (an Objective-C message's selector),
    as a ``function:`` spec matches any receiver ending in it; ``receivers``
    counts the callees before it (``""`` for a bare call), most frequent
    first. ``signal`` is the strongest of its calls' (``model.SIGNALS``).
    """

    method: str
    receivers: Mapping[str, int]
    signal: str
    calls: int
    first: Location
    # The first call's source text on one line, at most `EXAMPLE_MAX` characters.
    example: str


@dataclass(frozen=True)
class ForwardedParameter:
    """A parameter a forwarder passes on, and the targets it reaches there."""

    name: str
    index: int
    label: str | None
    targets: tuple[str, ...]


@dataclass(frozen=True)
class Callers:
    """The calls of a forwarder, found by name alone.

    Every call whose last callee segment is the name the forwarder is called
    by, in its file and elsewhere, but none in its own body: a call of that
    name there is the tracking call it forwards to, or itself. By name alone,
    so a call of another function of the same name counts too, a tracking
    call named like it included. ``first_unmatched`` is the first unmatched
    call in the forwarder's file, else the first one elsewhere.
    """

    same_file: int
    same_file_matched: int
    other_files: int
    other_files_matched: int
    first_unmatched: Location | None


@dataclass(frozen=True)
class Forwarder:
    """A function whose own parameter is a value a matched call could not read.

    The value is in its callers, so a call spec for this function reads them.
    ``function`` is the name they call it by: a Swift ``init``'s or a
    TypeScript ``constructor``'s is its type's (``ScreenView(screen:)``, ``new
    PageView(…)``). ``location`` is its file and declaration line; ``calls``
    counts the tracking calls in it that forward a parameter.
    """

    function: str
    location: Location
    parameters: tuple[ForwardedParameter, ...]
    calls: int
    callers: Callers


@dataclass(frozen=True)
class Inventory:
    """Everything ``tripl check --list`` shows. Nothing in it is ever sent."""

    root: Path
    # Matched call sites in scan order: files sorted, calls by offset.
    calls: tuple[ListedCall, ...]
    # Constant-signal groups first, then by calls (descending) and method.
    unmatched: tuple[UnmatchedGroup, ...]
    # By unmatched callers in the same file, then elsewhere (both descending).
    forwarders: tuple[Forwarder, ...]
    files: int
    skipped: tuple[SkippedFile, ...] = ()
    # Discovery stopped at `MAX_DISCOVERY_FILES` with source files left unread.
    truncated: bool = False


# --- the inventory ------------------------------------------------------------------
def inventory(config: CheckConfig, *, discovery: bool = False) -> Inventory:
    """Everything ``tripl check --list`` shows for ``config``, from ``scan``'s own passes.

    ``discovery`` is a run with no check config, scanning a whole repository:
    it reads at most ``MAX_DISCOVERY_FILES`` source files. With a config there
    is no cap; the user chose ``sources:``.
    """
    if discovery:
        files = discover(config, limit=MAX_DISCOVERY_FILES + 1)
        truncated = len(files) > MAX_DISCOVERY_FILES
        files = files[:MAX_DISCOVERY_FILES]
    else:
        files, truncated = discover(config), False
    tree = first_pass(config, files)
    listed: list[ListedCall] = []
    groups: dict[str, _Group] = {}
    tallies: dict[str, dict[str, _Tally]] = {}
    forwarders: dict[tuple[str, str, int], _Forwarding] = {}
    for found in second_pass(config, tree, explain=True):
        scopes = ScopeIndex(found.tokens, found.source.language, found.text)
        # A call `find_calls` reports with a parameter list's end is that
        # declaration, with a body or without one.
        declarations = scopes.bodiless.union(scope.signature_end for scope in scopes.functions)
        offsets: list[int] = []
        matched_spans: list[tuple[int, int]] = []
        # This file's forwarders, by the name their callers write.
        here: dict[str, list[_Forwarding]] = {}
        for call, hit in found.calls:
            if hit is None:
                continue
            if not offsets:
                offsets = [token.offset for token in found.tokens]
            at = bisect_left(offsets, call.offset)
            scope = scopes.innermost(at)
            matched = _listed(call, hit, found, scopes, scope, at)
            listed.append(matched)
            matched_spans.extend(_read_spans(call, hit[1]))
            if scope is not None:
                _forwarding(forwarders, here, matched, scope, found)
        spans = _Spans.of(matched_spans)
        # The calls of each name this file's forwarders are called by.
        named = {name: _Named() for name in here}
        for call, hit in found.calls:
            if call.selector is None and call.end not in declarations:
                name = call.callee.rpartition(".")[2]
                _count_call(tallies, name, call, hit is not None, found)
                if name in named:
                    named[name].add(bisect_left(offsets, call.offset), call, hit is not None)
            if hit is None and _candidate(call, spans, declarations):
                signal = _signal(call, found.evaluator)
                if signal is not None:
                    _group(groups, call, signal, found)
        for name, forwardings in here.items():
            for forwarding in forwardings:
                forwarding.own = named[name].outside(forwarding.scopes, found)
    return Inventory(
        root=config.root,
        calls=tuple(listed),
        unmatched=_unmatched(groups),
        forwarders=_forwarders(forwarders.values(), tallies),
        files=len(tree.parsed),
        skipped=tree.skipped,
        truncated=truncated,
    )


def _listed(
    call: Call,
    hit: tuple[EventTypeSpec, CallSpec],
    found: FileCalls,
    scopes: ScopeIndex,
    scope: FunctionScope | None,
    at: int,
) -> ListedCall:
    """One matched call, mapped as ``tripl check`` maps it, with what it read and why not.

    ``scope`` is the innermost function around it, ``at`` its first token.
    """
    event_type, spec = hit
    origin = call_origin(found.source, call, found.lines)
    collected = collect_call(call, spec, event_type, found.evaluator)
    items = tuple(call_items(collected, event_type.name, origin))
    fields = collected.fields or {}
    read = Read(
        name=None if collected.name is None else _refine(collected.name, scope, scopes, found, at),
        fields={key: _refine(value, scope, scopes, found, at) for key, value in fields.items()},
        properties=(
            None
            if collected.properties is None
            else _refine(collected.properties, scope, scopes, found, at)
        ),
        unmapped=_unmapped(call, spec),
        unread_object=_unread_object(call, spec, found.evaluator),
    )
    return ListedCall(
        origin=origin,
        event_type=event_type.name,
        read=read,
        dynamic=_reasons(items, read),
        default_label_fields=_default_label_fields(call, spec, event_type, fields),
        items=items,
    )


def _refine(
    value: Value, scope: FunctionScope | None, scopes: ScopeIndex, found: FileCalls, at: int
) -> ReadValue:
    """``value``, with the scan's own reason when it is a variable its function explains.

    The parameter check comes first, so Swift's ``guard let event = event``
    stays a parameter. A name of one segment that reads as a constant
    (``kEvent``, ``EVENT``) is the parameter it names, whatever its case: a
    parameter hides a constant of that name in the whole body. It is never a
    local: locals are found without regard to blocks, so one of that name may
    be out of scope at the call, and naming it would hide the advice a
    constant gets (add its file to ``enums:`` or ``sources:``); a variable has
    no advice to hide. Lambdas are no scopes: a lambda's own parameter stays a
    ``variable`` unless the named function around it has one of that name.
    """
    variable = value.reason == REASON_VARIABLE
    constant = value.reason == REASON_UNKNOWN_CONSTANT and len(value.path) == 1
    if not (variable or constant) or not value.path or scope is None:
        return ReadValue(value, value.reason)
    head = value.path[0]
    parameter = scope.parameter(head)
    if parameter is not None:
        reason = REASON_PARAMETER if len(value.path) == 1 else REASON_PARAMETER_PROPERTY
        line = found.lines.position(scope.offset)[0]
        return ReadValue(value, reason, Declared(scope.name, line, parameter))
    local = scopes.local_declaration(scope, at, head) if variable else None
    if local is not None:
        line = found.lines.position(found.tokens[local].offset)[0]
        return ReadValue(value, REASON_LOCAL, Declared(scope.name, line))
    return ReadValue(value, value.reason)


def _reasons(items: Sequence[CheckItem], read: Read) -> dict[str, str]:
    """One reason per dynamic target of ``items``, which all carry the same ``dynamic``.

    ``CheckItem.dynamic`` names a field by its bare name, so there a field
    ``name`` reads as the event's name. The targets are taken from ``read``
    instead, by ``call_items``' own rule (a name or field that is not known,
    properties that are no dictionary, else the name), keyed as
    ``Read.targets`` keys them.
    """
    if not items or not items[0].dynamic:
        return {}
    unread: list[tuple[str, ReadValue | None]] = [
        (target, value)
        for target, value in read.targets()
        if (value.value.kind != "entries" if target == PROPERTIES else not value.value.is_known)
    ]
    reasons: dict[str, str] = {}
    for target, value in unread or [(NAME, None)]:
        if value is None:
            reasons[target] = REASON_NOT_PASSED
        elif value.value.kind == "dynamic":
            reasons[target] = value.reason
        else:
            reasons[target] = REASON_WRONG_SHAPE
    return reasons


def _unmapped(call: Call, spec: CallSpec) -> tuple[str, ...]:
    """The labels (or ``#index``) of the arguments ``spec`` maps nowhere; ``null`` maps one."""
    unmapped: list[str] = []
    for arg in call.args:
        if not arg.tokens:
            continue  # the empty argument after a trailing comma
        if spec.object_arg is not None and arg.index == spec.object_arg and arg.label is None:
            continue  # read as the object argument
        mapped, _ = spec.target_for(arg)
        if not mapped:
            unmapped.append(arg.label if arg.label is not None else f"#{arg.index}")
    return tuple(unmapped)


def _unread_object(call: Call, spec: CallSpec, evaluator: Evaluator) -> Value | None:
    """The object argument as ``collect_call`` evaluates it, when the scan could not read it."""
    if spec.object_arg is None:
        return None
    for arg in call.args:
        if arg.index == spec.object_arg and arg.label is None and arg.tokens:
            value = evaluator.evaluate(arg.tokens)
            return value if value.kind == "dynamic" else None
    return None


def _default_label_fields(
    call: Call, spec: CallSpec, event_type: EventTypeSpec, fields: Mapping[str, Value]
) -> tuple[tuple[str, str], ...]:
    """``(field, label)`` for each field a label reached with no ``args:`` to say where it goes."""
    if spec.args is not None:
        return ()
    found: list[tuple[str, str]] = []
    for arg in call.args:
        if arg.label is None:
            continue
        if spec.objc_selector is not None:
            if arg.index < len(spec.positional):
                continue  # `positional:` says where it goes
            target = selector_field(arg.label)
        else:
            target = default_target(arg.label)
        resolved = event_type.resolve_target(target)
        if resolved in RESERVED_TARGETS:
            continue
        name = resolved.removeprefix(FIELD_PREFIX)
        if name in fields and (name, arg.label) not in found:
            found.append((name, arg.label))
    return tuple(found)


# --- calls that look like tracking but match no call spec ----------------------------
# Event plumbing, never tracking: the DOM's, any listener, a handler, an input
# event, a UI one-shot event holder.
_PLUMBING = frozenset(
    method.lower()
    for method in (
        "addEventListener",
        "removeEventListener",
        "dispatchEvent",
        "preventDefault",
        "stopPropagation",
        "createEvent",
        "initEvent",
    )
)
_HANDLER = re.compile(r"on[A-Z]")
_INPUT_EVENT = re.compile(r"(?:Touch|Key|Pointer|Motion|Drag|Input|Accessibility)Event")
# The DOM's event types, built to be dispatched (`new MouseEvent(…)`), as a
# bare call only: a bare `Event(…)` is also an Android view model's one-shot
# event holder, and `Analytics.Event(…)` may well be tracking.
_DOM_EVENTS = frozenset(
    {
        "Event",
        "CustomEvent",
        "UIEvent",
        "MouseEvent",
        "KeyboardEvent",
        "FocusEvent",
        "WheelEvent",
        "InputEvent",
        "PointerEvent",
        "TouchEvent",
        "DragEvent",
        "CompositionEvent",
        "ClipboardEvent",
        "AnimationEvent",
        "TransitionEvent",
        "MessageEvent",
        "ErrorEvent",
        "ProgressEvent",
        "PopStateEvent",
        "HashChangeEvent",
        "StorageEvent",
        "SubmitEvent",
        "BeforeUnloadEvent",
        "PageTransitionEvent",
    }
)
# Methods that take event constants without being tracking calls: formats,
# collection builders (`of` is Java's `List.of`, `Map.of`, `EnumSet.of` …)
# and `equals`.
_CONSTANT_SINKS = frozenset(
    {
        "format",
        "formatted",
        "stringWithFormat",
        "localizedStringWithFormat",
        "listOf",
        "setOf",
        "arrayOf",
        "mutableListOf",
        "mutableSetOf",
        "hashSetOf",
        "linkedSetOf",
        "sortedSetOf",
        "arrayListOf",
        "listOfNotNull",
        "setOfNotNull",
        "mapOf",
        "mutableMapOf",
        "hashMapOf",
        "of",
        "asList",
        "add",
        "addAll",
        "push",
        "equals",
    }
)
# ... and the ones that compare or keep a constant: a test's assertions and
# matchers (`expect`, `assertEquals`, `toBe`, `isEqualTo`, `shouldBe`, `eq`,
# `equalTo`) and React's hooks (`useState`). Spelled one way, so matched as written.
_CONSTANT_CHECKS = re.compile(r"(?:expect|assert|XCTAssert|should|to|is|use|eq|equal)(?:[A-Z]|$)")
_NAME_METHODS = frozenset({"capture", "screen", "screenview", "logscreenview", "logpurchase"})
_VENDORS = (
    "analytic",
    "tracker",
    "tracking",
    "amplitude",
    "mixpanel",
    "firebase",
    "appsflyer",
    "braze",
    "posthog",
    "snowplow",
    "metrica",
    "statsig",
    "rudder",
)
# Too common inside other words: only a whole receiver segment counts.
_VENDOR_SEGMENTS = frozenset({"segment", "adjust"})
_TRACKING_VERBS = (
    "log",
    "track",
    "send",
    "record",
    "report",
    "capture",
    "screen",
    "page",
    "identify",
    "alias",
    "group",
    "revenue",
    "purchase",
)
_CAMEL_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+")


@dataclass(frozen=True)
class _Spans:
    """What one file's matched calls read (``_read_spans``), for ``_candidate``.

    ``reach[i]`` is the furthest end among the first ``i + 1`` spans by
    start, so each question is one bisection over ``starts``, however the
    spans nest.
    """

    starts: tuple[int, ...]
    reach: tuple[int, ...]

    @classmethod
    def of(cls, spans: Iterable[tuple[int, int]]) -> _Spans:
        starts: list[int] = []
        reach: list[int] = []
        for start, end in sorted(spans):
            starts.append(start)
            reach.append(max(end, reach[-1]) if reach else end)
        return cls(tuple(starts), tuple(reach))

    def within(self, offset: int) -> bool:
        """``offset`` lies inside a span, at its start included."""
        before = bisect_right(self.starts, offset)
        return before > 0 and self.reach[before - 1] > offset

    def start_between(self, start: int, end: int) -> bool:
        """A span starts after ``start`` and before ``end``."""
        after = bisect_right(self.starts, start)
        return after < len(self.starts) and self.starts[after] < end


def _read_spans(call: Call, spec: CallSpec) -> Iterator[tuple[int, int]]:
    """Where a matched call is read: its callee and arguments, and each chain link ``spec`` maps.

    A builder-chain link runs from where the one before it ends to its own
    ``)``. One the spec does not map (``.then(…)``) is no part of the call, so
    a call in it is one of its own; a link mapped to null is part of it, as an
    argument mapped to null is mapped.
    """
    yield call.offset, call.end
    start = call.end
    for link in call.chain:
        if link.method in spec.chain:
            yield start, link.end
        start = link.end


def _candidate(call: Call, spans: _Spans, declarations: frozenset[int]) -> bool:
    """Whether an unmatched call may be listed: outside every matched call, and readable."""
    if spans.within(call.offset):
        return False  # in a matched call or a chain link its spec maps: listed with it
    if spans.start_between(call.offset, call.end):
        return False  # it carries a matched call, which is listed already
    if not call.args and not any(link.args for link in call.chain):
        return False  # nothing in it could be read
    return not (call.selector is None and call.end in declarations)  # a declaration


def _signal(call: Call, evaluator: Evaluator) -> str | None:
    """Why an unmatched call looks like tracking (one of ``model.SIGNALS``), or None.

    Case-insensitive, except a handler (``on`` and an upper-case letter), an
    input event, a DOM event type, an event holder, and an assertion or a
    hook a constant is passed to, which are spelled one way.
    """
    receivers, method = _callee_parts(call)
    lowered = method.lower()
    if lowered in _PLUMBING or lowered.endswith("listener"):
        return None
    if _HANDLER.match(method) or lowered.startswith("handle"):
        return None
    if _INPUT_EVENT.search(method) or method.endswith("LiveEvent") or method == "EventObserver":
        return None
    if not receivers and method in _DOM_EVENTS:
        return None
    if (
        method not in _CONSTANT_SINKS
        and not method.startswith("put")
        and not _CONSTANT_CHECKS.match(method)
        and _passes_event_constant(call, evaluator)
    ):
        return SIGNAL_CONSTANT
    words = [word.lower() for word in _CAMEL_WORD.findall(method)]
    if (
        "event" in lowered
        or any(word.startswith("track") for word in words)
        or lowered in _NAME_METHODS
    ):
        return SIGNAL_NAME
    if lowered.startswith(_TRACKING_VERBS) and any(_is_vendor(part) for part in receivers):
        return SIGNAL_RECEIVER
    return None


def _callee_parts(call: Call) -> tuple[list[str], str]:
    """``(receiver segments, method)``; an Objective-C message's method is its first part."""
    if call.selector is not None:
        return [call.callee], call.selector.split(":", 1)[0]
    parts = [part.replace("()", "").replace("[]", "") for part in call.callee.split(".")]
    return parts[:-1], parts[-1]


def _is_vendor(segment: str) -> bool:
    lowered = segment.lower()
    return lowered in _VENDOR_SEGMENTS or any(vendor in lowered for vendor in _VENDORS)


def _passes_event_constant(call: Call, evaluator: Evaluator) -> bool:
    """An argument is a constant the index reads, on a path that names an event."""
    for arg in call.args:
        path = pure_path(arg.tokens)
        if path is None or len(path) < 2:
            continue
        if not any("event" in segment.lower() for segment in path[:-1]):
            continue
        value = evaluator.evaluate(arg.tokens)
        if value.kind == "text" and not value.interpolated:
            return True
    return False


@dataclass
class _Group:
    signal: str
    first: Location
    example: str
    calls: int = 0
    receivers: dict[str, int] = field(default_factory=dict)


def _group(groups: dict[str, _Group], call: Call, signal: str, found: FileCalls) -> None:
    if call.selector is not None:
        method, receiver = call.selector, call.callee
    else:
        receiver, _, method = call.callee.rpartition(".")
    group = groups.get(method)
    if group is None:
        line, column = found.lines.position(call.offset)
        first = Location(found.source.relative, line, column)
        example = clip(found.text[call.offset : call.end], EXAMPLE_MAX)
        group = groups[method] = _Group(signal, first, example)
    elif SIGNALS.index(signal) < SIGNALS.index(group.signal):
        group.signal = signal
    group.calls += 1
    group.receivers[receiver] = group.receivers.get(receiver, 0) + 1


def _unmatched(groups: Mapping[str, _Group]) -> tuple[UnmatchedGroup, ...]:
    ordered = sorted(
        groups.items(),
        key=lambda item: (item[1].signal != SIGNAL_CONSTANT, -item[1].calls, item[0]),
    )
    return tuple(
        UnmatchedGroup(
            method=method,
            receivers=dict(sorted(group.receivers.items(), key=lambda item: (-item[1], item[0]))),
            signal=group.signal,
            calls=group.calls,
            first=group.first,
            example=group.example,
        )
        for method, group in ordered
    )


# --- forwarders ---------------------------------------------------------------------
# A Swift `init` and a TypeScript `constructor` are called by their type's name.
_INITIALIZERS = frozenset({("swift", "init"), ("ts", "constructor")})


@dataclass
class _Tally:
    """Calls of one name, in one file."""

    calls: int = 0
    matched: int = 0
    first_unmatched: Location | None = None

    def add(self, call: Call, matched: bool, found: FileCalls) -> None:
        self.calls += 1
        if matched:
            self.matched += 1
        elif self.first_unmatched is None:
            line, column = found.lines.position(call.offset)
            self.first_unmatched = Location(found.source.relative, line, column)


@dataclass
class _Forwarding:
    """What a ``Forwarder`` reports, gathered while the scan reads its file."""

    function: str
    location: Location
    # Its scopes in that file: one, unless two functions of its name are
    # declared on its line.
    scopes: list[FunctionScope] = field(default_factory=list)
    calls: int = 0
    parameters: dict[str, tuple[Parameter, list[str]]] = field(default_factory=dict)
    # Its calls in its own file, the ones in its own body left out.
    own: _Tally = field(default_factory=_Tally)


@dataclass
class _Named:
    """The calls of one name in one file, in offset order, to count a forwarder's callers by.

    A forwarder's body is a range of token indexes, so the calls in it are
    found by bisection: one per body, however many calls and overloads of
    its name the file holds.
    """

    # Each call's first token index.
    at: list[int] = field(default_factory=list)
    # `matched[i]`: how many of the first `i` calls are matched.
    matched: list[int] = field(default_factory=lambda: [0])
    # The unmatched calls, and their first token indexes.
    unmatched: list[Call] = field(default_factory=list)
    unmatched_at: list[int] = field(default_factory=list)

    def add(self, index: int, call: Call, matched: bool) -> None:
        self.at.append(index)
        self.matched.append(self.matched[-1] + matched)
        if not matched:
            self.unmatched.append(call)
            self.unmatched_at.append(index)

    def outside(self, scopes: Iterable[FunctionScope], found: FileCalls) -> _Tally:
        """These calls, the ones the bodies of ``scopes`` hold left out."""
        bodies = _bodies(scopes)
        calls, matched = len(self.at), self.matched[-1]
        for low, high in bodies:
            first, last = bisect_right(self.at, low), bisect_left(self.at, high)
            calls -= last - first
            matched -= self.matched[last] - self.matched[first]
        # The first unmatched call before a body, between two, or after the last.
        position = 0
        for low, high in bodies:
            if position == len(self.unmatched_at) or self.unmatched_at[position] <= low:
                break
            position = max(position, bisect_left(self.unmatched_at, high))
        first_unmatched = None
        if position < len(self.unmatched):
            line, column = found.lines.position(self.unmatched[position].offset)
            first_unmatched = Location(found.source.relative, line, column)
        return _Tally(calls, matched, first_unmatched)


def _bodies(scopes: Iterable[FunctionScope]) -> list[tuple[int, int]]:
    """The bodies of ``scopes``, sorted, the ones that overlap merged into one.

    A token is in a body when it lies strictly between its two ends
    (``FunctionScope.holds``), so two bodies meet only when the second starts
    before the first ends.
    """
    merged: list[tuple[int, int]] = []
    for low, high in sorted(scope.body for scope in scopes):
        if merged and low < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(high, merged[-1][1]))
        else:
            merged.append((low, high))
    return merged


def _count_call(
    tallies: dict[str, dict[str, _Tally]], name: str, call: Call, matched: bool, found: FileCalls
) -> None:
    per_file = tallies.setdefault(name, {})
    tally = per_file.get(found.source.relative)
    if tally is None:
        tally = per_file[found.source.relative] = _Tally()
    tally.add(call, matched, found)


def _forwarding(
    forwarders: dict[tuple[str, str, int], _Forwarding],
    here: dict[str, list[_Forwarding]],
    call: ListedCall,
    scope: FunctionScope,
    found: FileCalls,
) -> None:
    """Count ``call`` for ``scope``, the function it sits in, when it forwards a parameter.

    It does when a target's value is a bare parameter (reason ``parameter``),
    never ``parameter_property``: a property of a parameter is set where its
    object is built, and a call spec for the function would only add calls
    whose value is not read either. An initializer of a type the scan cannot
    name is no forwarder: what its callers write is unknown.
    """
    forwarded: list[tuple[str, Parameter]] = []
    for target, read in call.read.targets():
        declared = read.declared
        if read.reason != REASON_PARAMETER or declared is None or declared.parameter is None:
            continue
        forwarded.append((target, declared.parameter))
    if not forwarded:
        return
    initializer = (found.source.language, scope.name) in _INITIALIZERS
    function = scope.owner if initializer else scope.name
    if function is None:
        return
    path = found.source.relative
    line = found.lines.position(scope.offset)[0]
    forwarding = forwarders.get((function, path, line))
    if forwarding is None:
        forwarding = _Forwarding(function, Location(path, line))
        forwarders[(function, path, line)] = forwarding
        here.setdefault(function, []).append(forwarding)
    if scope not in forwarding.scopes:
        forwarding.scopes.append(scope)
    forwarding.calls += 1
    for target, parameter in forwarded:
        _, targets = forwarding.parameters.setdefault(parameter.name, (parameter, []))
        if target not in targets:
            targets.append(target)


def _forwarders(
    forwarders: Iterable[_Forwarding], tallies: Mapping[str, Mapping[str, _Tally]]
) -> tuple[Forwarder, ...]:
    """The functions whose own parameter reaches a matched call, with their callers."""
    found = [
        Forwarder(
            function=forwarding.function,
            location=forwarding.location,
            parameters=tuple(
                ForwardedParameter(item.name, item.index, item.label, tuple(targets))
                for item, targets in sorted(
                    forwarding.parameters.values(), key=lambda entry: entry[0].index
                )
            ),
            calls=forwarding.calls,
            callers=_callers(
                forwarding.own, tallies.get(forwarding.function, {}), forwarding.location.path
            ),
        )
        for forwarding in forwarders
    ]
    found.sort(
        key=lambda forwarder: (
            -(forwarder.callers.same_file - forwarder.callers.same_file_matched),
            -(forwarder.callers.other_files - forwarder.callers.other_files_matched),
            forwarder.function,
            forwarder.location.path,
            forwarder.location.line,
        )
    )
    return tuple(found)


def _callers(own: _Tally, per_file: Mapping[str, _Tally], path: str) -> Callers:
    """A forwarder's calls by name: ``own`` in its file ``path``, the rest from ``per_file``."""
    others = [tally for other, tally in per_file.items() if other != path]
    first = own.first_unmatched or next(
        (tally.first_unmatched for tally in others if tally.first_unmatched is not None), None
    )
    return Callers(
        same_file=own.calls,
        same_file_matched=own.matched,
        other_files=sum(tally.calls for tally in others),
        other_files_matched=sum(tally.matched for tally in others),
        first_unmatched=first,
    )


# --- the counts ---------------------------------------------------------------------
@dataclass(frozen=True)
class Summary:
    """What ``--list`` counts, the same in its text and in its JSON document.

    Every count is of call sites. ``reasons`` counts the very (call, target)
    pairs ``dynamic`` does, so a target's codes add up to its count there.
    """

    calls: int
    items: int
    fully_read: int
    not_sent: int
    # Per target: the calls whose value for it was not read.
    dynamic: Mapping[str, int]
    # Per target, then reason code.
    reasons: Mapping[str, Mapping[str, int]]
    # Per event type, then target: the calls carrying a value for it, read or not.
    targets: Mapping[str, Mapping[str, int]]
    # Per event type: each `(field, label)` a label reached by the default mapping.
    default_label_fields: Mapping[str, tuple[tuple[str, str], ...]]


def summarize(found: Inventory) -> Summary:
    """The counts of ``found``'s listed calls, keys in the order the calls first show them."""
    dynamic: dict[str, int] = {}
    reasons: dict[str, dict[str, int]] = {}
    targets: dict[str, dict[str, int]] = {}
    defaults: dict[str, list[tuple[str, str]]] = {}
    for call in found.calls:
        for target, code in call.dynamic.items():
            dynamic[target] = dynamic.get(target, 0) + 1
            codes = reasons.setdefault(target, {})
            codes[code] = codes.get(code, 0) + 1
        carried = targets.setdefault(call.event_type, {})
        for target, _ in call.read.targets():
            carried[target] = carried.get(target, 0) + 1
        for pair in call.default_label_fields:
            pairs = defaults.setdefault(call.event_type, [])
            if pair not in pairs:
                pairs.append(pair)
    return Summary(
        calls=len(found.calls),
        items=sum(len(call.items) for call in found.calls),
        fully_read=sum(1 for call in found.calls if call.fully_read),
        not_sent=sum(1 for call in found.calls if not call.sent),
        dynamic=dynamic,
        reasons=reasons,
        targets=targets,
        default_label_fields={key: tuple(pairs) for key, pairs in defaults.items()},
    )
