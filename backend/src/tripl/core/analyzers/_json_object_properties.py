"""Which JSON-path properties a person documented, for the object fold (F23).

A scan folds a nested object's leaves into one object property
(``json_paths.object_property_paths``). Folding rewrites the event templates,
so a dotted property that is only the scan's own — never edited, on no event's
property list — loses its last ``${token}`` and is retired by the sweep like
any other orphan. A property a person made theirs must not be folded away:
its path is handed to the planner as documented, which keeps a documented leaf
dotted and folds a documented object exactly where it is.

"Made theirs" is ``variable_retirement.human_claim`` — the sweep's own
predicate, so the fold never drops a property the sweep would keep for a
person — plus a place on an event's property list, the per-event entries a
person writes (F23.3). An exclusion is not a claim here: an excluded path is
one the person asked the scan to stop tracking, and folding it into its
object is exactly that.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.analyzers._event_generator_variables import VariableIndex
from tripl.core.variable_retirement import KeptReason, human_claim
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride


def documented_json_paths(
    session: Session,
    *,
    project_id: uuid.UUID,
    index: VariableIndex,
) -> set[str]:
    """Every dotted warehouse token of a variable a person documented.

    Dotted is all the planner needs: it only reads tokens under one of its
    JSON columns, and a scalar column's name never has a path below it.
    """
    variables = [
        variable
        for variable in index.variables()
        if not variable.excluded_from_scans
        and any("." in token for token in VariableIndex.source_tokens_of(variable))
    ]
    if not variables:
        return set()
    listed = set(
        session.execute(
            select(VariableEventValueOverride.variable_id)
            .where(
                VariableEventValueOverride.project_id == project_id,
                VariableEventValueOverride.variable_id.in_([v.id for v in variables]),
            )
            .distinct()
        ).scalars()
    )
    documented: set[str] = set()
    for variable in variables:
        claim = human_claim(variable)
        if variable.id not in listed and claim in (None, KeptReason.EXCLUDED):
            continue
        documented.update(
            token for token in VariableIndex.source_tokens_of(variable) if "." in token
        )
    return documented


def is_object_property(variable: Variable) -> bool:
    """A ``json`` variable whose schema says it holds one object (F23.4e).

    What the samplers ask whole objects for, and what replay must not extract
    as a grouping value.
    """
    schema = variable.json_schema or {}
    return variable.variable_type == "json" and schema.get("type") == "object"
