"""The shared 409 is rendered verbatim in the web UI, so it speaks the web UI's nouns.

``name_format_conflict_detail`` writes ONE sentence for three delete doors and
three clients: the web UI, ``tripl drifts accept``, and the MCP server. The web
UI renders it untouched — ``api/client.ts`` puts the ``detail`` string into
``ApiError.message`` and ``EventTypesTab``/``EventDriftBadge`` drop that straight
into a ``role="alert"`` — so whatever is written here is UI copy, whoever else
also reads it.

That is a blind spot in the frontend's own vocabulary guard, and the reason this
file lives on the backend side. ``frontend/src/scan-docs-agreement.test.ts``
enforces the vocabulary settlement (the web UI says *scan* and *run*; the wire
keeps ``scan_config`` and ``job``) by reading string literals and JSX text out of
frontend sources. A sentence assembled in Python has no literal there to find, so
"1 scan config(s)" sat in a ``role="alert"`` through the whole epic that banned
it.

Rather than copy the frontend's rule into a Python literal — two copies of a rule
is how the rule ends up enforcing two different things, and that pattern has
already been widened once, for the "configuration" spelling — this reads
``SCAN_CONFIG_NOUN`` out of the frontend guard and applies it to the string the
backend builds. Parsing TypeScript from a Python test is ugly; it is less ugly
than a second, quieter definition of the same ban. Same shape as
``test_alert_template_mirror`` and ``test_cli_constant_mirror``.

**Scope is the sentence this helper authors.** The ``lead`` and ``then`` clauses
are the callers' words, passed in from ``field_service``, ``schema_drift_service``
and ``plan_branch_merge_service``, and the same rule applies to them — but the
only way to assert on them here is to restate the literals, and a restated
literal drifts from the call site in exactly the way this whole module exists to
prevent. They are short, they name an action, and none of them names this record
at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tripl.models.scan_config import ScanConfig
from tripl.services.scan_config_lookup import name_format_conflict_detail

_FRONTEND_GUARD = Path(__file__).resolve().parents[4] / "frontend/src/scan-docs-agreement.test.ts"

# `const SCAN_CONFIG_NOUN = /\bscan config(?:uration)?s?\b/i`
_TS_NOUN = re.compile(
    r"^const SCAN_CONFIG_NOUN = /(?P<body>.+)/(?P<flags>[a-z]*)\s*$", re.MULTILINE
)


def _banned_noun() -> re.Pattern[str]:
    """The frontend guard's noun pattern, recompiled for ``re``.

    The pattern is written in the subset both engines share (``\\b``, a
    non-capturing group, ``?``), so translation is the ``i`` flag and nothing
    else. If it ever grows a JS-only construct, ``re.compile`` raises and this
    fails loudly rather than degrading into a pattern that matches less.
    """
    if not _FRONTEND_GUARD.is_file():  # pragma: no cover - only in a partial checkout
        pytest.skip(f"frontend vocabulary guard not present at {_FRONTEND_GUARD}")
    match = _TS_NOUN.search(_FRONTEND_GUARD.read_text(encoding="utf-8"))
    assert match, (
        f"{_FRONTEND_GUARD.name} no longer declares SCAN_CONFIG_NOUN as a single-line regex "
        "literal, so this test cannot read the rule it enforces. Re-point it at whatever "
        "replaced the constant — do not settle for a hand-copied pattern here."
    )
    flags = re.IGNORECASE if "i" in match.group("flags") else 0
    try:
        return re.compile(match.group("body"), flags)
    except re.error as exc:  # pragma: no cover - only if the TS pattern goes JS-only
        pytest.fail(f"SCAN_CONFIG_NOUN is no longer portable to Python re: {exc}")


def _configs(count: int) -> list[ScanConfig]:
    """Unsaved rows: the helper is pure and reads only ``name`` and ``event_name_format``."""
    return [
        ScanConfig(name=f"Old events {index}", event_name_format="{action}")
        for index in range(count)
    ]


def _detail(count: int) -> str:
    return name_format_conflict_detail(
        field_name="action",
        configs=_configs(count),
        lead="Cannot delete this field.",
        then="delete the field",
    )


def test_the_frontend_guard_was_found_and_parsed() -> None:
    """Guard the guard: a pattern that silently matches nothing proves nothing."""
    pattern = _banned_noun()
    assert pattern.search("2 scan configs"), (
        f"the pattern read out of {_FRONTEND_GUARD.name} no longer matches the noun it bans, "
        "so every assertion below would pass vacuously"
    )


@pytest.mark.parametrize("count", [1, 2, 5])
def test_the_shared_409_never_shows_a_web_user_the_wire_noun(count: int) -> None:
    detail = _detail(count)
    assert not _banned_noun().search(detail), (
        f"the 409 body calls a scan a 'scan config': {detail!r}. EventTypesTab and "
        "EventDriftBadge render this string verbatim in a role=alert, so it is web-UI copy "
        "and the settlement fixed that noun as 'scan'. scan-docs-agreement.test.ts cannot see "
        "it from here, which is why this test exists."
    )


@pytest.mark.parametrize("count", [1, 2, 5])
def test_the_shared_409_never_pluralises_with_a_parenthesised_s(count: int) -> None:
    detail = _detail(count)
    assert "(s)" not in detail, (
        f"the 409 body pluralises with '(s)': {detail!r}. The count is known when the sentence "
        "is built, and the frontend replaced this shape everywhere the frontend could reach "
        "(lib/plural.ts countOf) — a string the frontend cannot reach still gets read by the "
        "same user."
    )


def test_the_counted_noun_agrees_with_the_number_of_scans() -> None:
    """Spelling both forms out is the point; a stray 's' is the defect it replaced."""
    assert "format of 1 scan: " in _detail(1)
    assert "format of 2 scans: " in _detail(2)


def test_the_sentence_still_names_every_blocking_scan_and_the_unblocking_edit() -> None:
    """The vocabulary fix must not have cost the message its content."""
    detail = _detail(2)
    assert "'Old events 0' ({action})" in detail
    assert "'Old events 1' ({action})" in detail
    assert "Edit their Event name formats" in detail


def test_the_back_references_agree_with_the_count_too() -> None:
    """Pluralising only the counted noun leaves the rest of the sentence lying.

    The first cut of this fix produced "the event name format of 2 scans: 'A';
    'B'. Without it THE SCAN cannot build an event name ... Edit THE SCAN'S
    Event name format" — a sentence that names two scans and then instructs the
    reader about one. That is the "(s)" defect moved two clauses along, not
    fixed.
    """
    one = _detail(1)
    assert "the scan cannot build an event name" in one
    assert "Edit the scan's Event name format" in one

    many = _detail(2)
    assert "those scans cannot build event names" in many
    assert "Edit their Event name formats" in many
    assert "the scan cannot build an event name" not in many
    assert "Edit the scan's Event name format" not in many
