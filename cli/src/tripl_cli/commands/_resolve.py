"""``<name-or-id>`` selectors: ONE matcher, one refusal, for every command taking one.

Three arguments across the CLI accept either a name or an id — ``tripl scans``'
``<scan>``, ``tripl plan fields``' ``<event-type>`` and ``--branch`` on every
plan read — and all three resolve through ``api.scans.resolve_selectors``, which
``tripl watch``'s ``--scan`` already used. Exact on the name first, then on the
id; never a substring, never case-insensitive.

That rule is not fussiness. A ``scans run`` that triggered the wrong SQL because
two config names share a prefix is worse than one that refuses to start, and a
``--branch`` that silently guessed would answer plan questions about the wrong
revision — which is the same class of wrong answer, on a read, and quieter. So
a selector matching nothing lists the candidates and exits 2, and a selector
matching two names them both and exits 2.
"""

from __future__ import annotations

from collections.abc import Mapping

from tripl_cli.api.scans import resolve_selectors
from tripl_cli.errors import TriplConfigError


def resolve_one(named: Mapping[str, str], selector: str, *, what: str) -> tuple[str, str]:
    """``{id: name}`` plus one selector -> ``(id, name)``. Never guesses.

    ``what`` names the thing in both refusals — "scan config", "event type",
    "branch" — so the message an operator reads says which argument was wrong
    rather than which function raised.
    """
    matched, unmatched = resolve_selectors(named, [selector])
    if unmatched:
        raise _unresolved(selector, named, what)
    if len(matched) > 1:
        # `what` is never pluralised, here or below. English pluralisation is
        # not a thing a message builder should be guessing at ("branchs"), and
        # `render.plural` guesses the same way.
        raise TriplConfigError(
            f"{selector!r} matches {len(matched)} candidates "
            f"({', '.join(matched)}); name the {what} by id."
        )
    return matched[0], named[matched[0]]


def _unresolved(selector: str, named: Mapping[str, str], what: str) -> TriplConfigError:
    """The rule, then the candidates.

    The candidates are printed in full rather than as a "did you mean": the list
    is short, the operator is at a terminal, and a suggestion that guesses wrong
    is the behaviour this whole module refuses.
    """
    candidates = [f"  {name} ({identifier})" for identifier, name in named.items()]
    listing = "\n".join(candidates) if candidates else "  (none exist in this project)"
    return TriplConfigError(
        f"no {what} matches {selector!r}. The match is exact, on the name first and "
        f"then the id. Candidates:\n{listing}"
    )
