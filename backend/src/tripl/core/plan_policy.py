"""What an extension's plan policies are asked, and what they answer.

The core has its own per-project gates (minimum approvals, self-approval,
event-type owners, reviewers). An extension may add rules of its own through
:meth:`tripl.extensions.Extension.plan_policy_violations`, which the core asks
at three points, each a :data:`PolicyPhase`:

* ``validate`` — ``POST /projects/{slug}/plan/validate``, the route ``tripl
  check`` and CI call. The context carries the batch's calls; every violation
  names the call it is about by ``item_ref`` and is reported as a
  ``policy_violation`` finding on that call. One that names no call is dropped.
* ``merge`` — a branch merge, after the core's own gates passed. The context
  carries the branch's merge base and its current plan as plan snapshots
  (``plan_revision_service.build_plan_snapshot``) and who approved the
  branch's current content. A blocking violation refuses the merge with 409
  ``policy_violations``.
* ``direct_edit`` — a write to the main plan (a plan write route called
  without ``?branch=``). A blocking violation refuses the write with 409
  ``policy_violations``; writes to a branch are never asked about.

Plain data only, so a policy is testable without a database and the core never
imports an extension's types.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

PolicyPhase = Literal["validate", "merge", "direct_edit"]
PolicySeverity = Literal["error", "warning"]


@dataclass(frozen=True)
class PolicyViolation:
    """One rule broken. ``error`` blocks; ``warning`` is only reported.

    ``rule`` is a stable machine name (``naming.event``, say) and ``message``
    the sentence a person reads. ``approver_ids`` lists the users whose
    approval of the branch would clear the violation, when it is the kind an
    approval clears; the core only passes it on.
    """

    rule: str
    message: str
    severity: PolicySeverity = "error"
    item_ref: str | None = None
    entity_type: str | None = None
    entity: str | None = None
    field: str | None = None
    approver_ids: tuple[uuid.UUID, ...] = ()

    @property
    def blocking(self) -> bool:
        return self.severity == "error"

    def as_json(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "message": self.message,
            "severity": self.severity,
            "entity_type": self.entity_type,
            "entity": self.entity,
            "field": self.field,
            "approver_ids": [str(user_id) for user_id in self.approver_ids],
        }


@dataclass(frozen=True)
class PolicyCall:
    """One call of a ``validate`` batch, as the plan resolved it.

    ``event_type`` is the plan's event type name when the call resolved to one,
    else what the call sent. ``name`` is the call's own name or identity, with
    ``${...}`` holes for parts only known at runtime. ``field_names`` are the
    keys the call sends, its ``fields`` and its ``properties`` together.
    ``complete`` is true when the call is a whole captured payload, so a key it
    lacks is really absent.
    """

    ref: str
    event_type: str | None
    name: str | None
    identity: str | None
    event_id: uuid.UUID | None
    field_names: tuple[str, ...]
    complete: bool


@dataclass(frozen=True)
class PlanPolicyContext:
    """Where the question is asked. Fields a phase does not fill stay empty.

    ``branch_id`` is ``None`` for main. ``actor_id`` is the user asking (the
    merger, the editor, the caller of ``validate``). For ``merge``,
    ``author_id`` is who opened the branch and ``approver_ids`` who approved its
    current content (approvals of earlier content do not count).
    """

    phase: PolicyPhase
    organization_id: uuid.UUID
    project_id: uuid.UUID
    project_slug: str
    branch_id: uuid.UUID | None = None
    actor_id: uuid.UUID | None = None
    calls: Sequence[PolicyCall] = ()
    base_snapshot: Mapping[str, Any] | None = None
    branch_snapshot: Mapping[str, Any] | None = None
    author_id: uuid.UUID | None = None
    approver_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)


def blocking(violations: Sequence[PolicyViolation]) -> list[PolicyViolation]:
    return [violation for violation in violations if violation.blocking]


def refusal_detail(violations: Sequence[PolicyViolation]) -> dict[str, Any]:
    """The 409 body a refused merge or main write answers with."""
    count = len(violations)
    noun = "rule" if count == 1 else "rules"
    return {
        "message": f"Blocked by {count} plan {noun}: "
        + " ".join(violation.message for violation in violations),
        "policy_violations": [violation.as_json() for violation in violations],
    }
