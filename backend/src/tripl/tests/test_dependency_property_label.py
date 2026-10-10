"""The impact summary names a variable the way the product does: a property.

The "Used by" panel puts this sentence over edge sentences that already say
"property"; the headline was the last place still saying "variable".
"""

from __future__ import annotations

import uuid

from tripl.services._dependency_model import Edge, impact_summary


def _edge(kind: str) -> Edge:
    return Edge(kind=kind, id=uuid.uuid4(), name="x", relation="r", certainty="direct")


def test_one_property_is_singular() -> None:
    assert impact_summary([_edge("event"), _edge("variable")]) == "1 event and 1 property"


def test_several_properties_are_plural() -> None:
    assert impact_summary([_edge("variable"), _edge("variable")]) == "2 properties"
