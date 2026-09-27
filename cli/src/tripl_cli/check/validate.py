"""Items -> verdicts: de-duplicate, batch to ``POST /plan/validate``, map verdicts back.

The same event is usually tracked from many call sites, so identical items are
sent ONCE and their verdict is fanned back out to every site. Refs on the wire
are this module's own (``i0``, ``i1``, …); the route echoes them, and a verdict
whose ref the CLI never sent is ignored rather than trusted.

``--strict`` findings are added here, not by the route: whether a value was
knowable at scan time is a fact about the source, which only the CLI has seen.
``--strict`` is also sent to the route, which then notes runtime-only values as
``info`` findings. An ``info`` finding never changes an item's status on its
own — except under ``--strict``, where it makes an otherwise clean item a
warning (and so exit 1).

Every item is held to the route's size limits before it is sent
(``payloads.enforce_limits``); what had to be cut is reported on the item as an
``oversize_value`` warning.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from tripl_cli.api import plan_validation
from tripl_cli.check.model import (
    CODE_DYNAMIC_VALUE,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    STATUS_DYNAMIC,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_WARNING,
    CheckFinding,
    CheckItem,
    CheckResult,
)
from tripl_cli.check.payloads import enforce_limits
from tripl_cli.diagnostics.collect import Reader
from tripl_cli.model import JsonDict, as_list, text_of

_STATUSES = frozenset({STATUS_OK, STATUS_WARNING, STATUS_ERROR})
_SEVERITIES = frozenset({SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO})


def unique_items(items: Sequence[CheckItem]) -> tuple[list[JsonDict], list[int]]:
    """``(wire items with refs, index into them per input item or -1 when not sent)``."""
    wire: list[JsonDict] = []
    by_key: dict[str, int] = {}
    positions: list[int] = []
    for item in items:
        if not item.sendable:
            positions.append(-1)
            continue
        body = item.wire()
        key = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
        index = by_key.get(key)
        if index is None:
            index = len(wire)
            by_key[key] = index
            wire.append({"ref": f"i{index}", **body})
        positions.append(index)
    return wire, positions


async def validate(
    reader: Reader,
    slug: str,
    items: Sequence[CheckItem],
    *,
    branch_id: str | None,
    strict: bool,
    batch_size: int = plan_validation.MAX_ITEMS,
) -> tuple[CheckResult, ...]:
    items = [enforce_limits(item) for item in items]
    wire, positions = unique_items(items)
    verdicts: dict[str, JsonDict] = {}
    for batch in plan_validation.batches(wire, batch_size):
        response = await reader.send(
            plan_validation.validate(slug, batch, branch=branch_id, strict=strict)
        )
        sent = {entry["ref"] for entry in batch}
        for verdict in plan_validation.verdicts(response):
            ref = text_of(verdict, "ref")
            if ref is not None and ref in sent:
                verdicts[ref] = verdict
    results: list[CheckResult] = []
    for item, position in zip(items, positions, strict=True):
        found = verdicts.get(f"i{position}") if position >= 0 else None
        results.append(result_for(item, found, strict=strict, sent=position >= 0))
    return tuple(results)


def result_for(
    item: CheckItem, verdict: JsonDict | None, *, strict: bool, sent: bool
) -> CheckResult:
    findings = [*item.notes, *_findings(verdict)]
    if not sent:
        status = STATUS_DYNAMIC
    elif verdict is None:
        # Sent but not answered: the route dropped it. Loud, because a silent
        # pass here would be the checker approving what it never checked.
        findings.append(
            CheckFinding(
                code="no_verdict",
                severity=SEVERITY_ERROR,
                message="the validator returned no verdict for this item",
            )
        )
        status = STATUS_ERROR
    else:
        raw_status = text_of(verdict, "status")
        status = (
            raw_status if raw_status is not None and raw_status in _STATUSES else _worst(findings)
        )
        if status == STATUS_OK and _worst(item.notes) != STATUS_OK:
            status = STATUS_WARNING
        if (
            strict
            and status == STATUS_OK
            and any(finding.severity == SEVERITY_INFO for finding in findings)
        ):
            status = STATUS_WARNING
    if strict and item.dynamic:
        known = {(finding.code, finding.field) for finding in findings}
        for target in item.dynamic:
            field = None if target == "name" else target
            if (CODE_DYNAMIC_VALUE, field) in known:
                continue
            findings.append(
                CheckFinding(
                    code=CODE_DYNAMIC_VALUE,
                    severity=SEVERITY_WARNING,
                    field=field,
                    message=(
                        "the event name is not known until runtime"
                        if field is None
                        else f"the value of {field!r} is not known until runtime"
                    ),
                )
            )
        if status in (STATUS_OK, STATUS_DYNAMIC):
            status = STATUS_WARNING
    return CheckResult(
        item=item,
        status=status,
        findings=tuple(findings),
        event_id=text_of(verdict, "event_id") if verdict is not None else None,
        identity=text_of(verdict, "identity") if verdict is not None else None,
    )


def _findings(verdict: JsonDict | None) -> list[CheckFinding]:
    if verdict is None:
        return []
    found: list[CheckFinding] = []
    for raw in as_list(verdict.get("findings")):
        severity = text_of(raw, "severity") or SEVERITY_ERROR
        found.append(
            CheckFinding(
                code=text_of(raw, "code") or "unknown",
                severity=severity if severity in _SEVERITIES else SEVERITY_ERROR,
                field=text_of(raw, "field"),
                message=text_of(raw, "message") or "",
            )
        )
    return found


def _worst(findings: Sequence[CheckFinding]) -> str:
    """The status ``findings`` add up to; ``info`` counts as ok."""
    severities = {finding.severity for finding in findings}
    if SEVERITY_ERROR in severities:
        return STATUS_ERROR
    if SEVERITY_WARNING in severities:
        return STATUS_WARNING
    return STATUS_OK


def summary(results: Sequence[CheckResult]) -> dict[str, int]:
    counts = {"checked": len(results), "ok": 0, "warnings": 0, "errors": 0, "dynamic": 0}
    key = {
        STATUS_OK: "ok",
        STATUS_WARNING: "warnings",
        STATUS_ERROR: "errors",
        STATUS_DYNAMIC: "dynamic",
    }
    for result in results:
        counts[key.get(result.status, "errors")] += 1
    return counts
