"""Guard the troubleshooting docs against drifting from the suppression rule.

``_SUPPRESSING_INBOX_STATUSES`` decides which inbox decisions stop a delivery,
and two troubleshooting checklists MIRROR it in prose: the "Alerts never fire"
item in ``website/docs/use/troubleshooting.md`` and the "My alert never fired"
note in ``website/docs/use/user-guide.md``. Both exist to answer "the rule is on,
the destination is on, and nothing arrives" — so a status missing from either
list is not a typo, it is a page that cannot diagnose the case it was written for.

That is not hypothetical. ``acknowledged`` joined the tuple when an operator who
had acked an incident kept being paged for it every hour, and
both checklists were left naming only three of the four. For months the docs
therefore told a reader whose alerts were silenced by their own acknowledge to go
look at cooldowns and destinations, and nothing in either suite noticed.
Prose cannot be type-checked, but it can be required to contain the
words.

Same shape as ``test_cli_constant_mirror.py``: structural, cheap, reads the docs
as TEXT, and names both sides of the mismatch so the failure message alone is
enough to fix it. It lives in the backend suite for the same reason that one
does — this is the CI job with the whole repository checked out.

It deliberately asserts only PRESENCE, in the right section. Wording, ordering
and emphasis stay the writer's business; what may not happen again is a status
that suppresses deliveries and is nowhere on the page people bring that symptom
to.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tripl.worker.tasks.metrics.dispatch import _SUPPRESSING_INBOX_STATUSES

# tests -> tripl -> src -> backend -> repository root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DOCS = _REPO_ROOT / "website" / "docs" / "use"

# Each entry: the file, and the heading whose section must name every status.
# The user-guide note is an admonition rather than a heading, which is why the
# anchor is matched as a literal line prefix instead of as markdown structure.
_CHECKLISTS = (
    pytest.param(
        _DOCS / "troubleshooting.md",
        "## Alerts never fire",
        id="troubleshooting-alerts-never-fire",
    ),
    pytest.param(
        _DOCS / "user-guide.md",
        ':::note "My alert never fired"',
        id="user-guide-my-alert-never-fired",
    ),
)


def _section(text: str, anchor: str) -> str:
    """The anchored section, up to whatever ends it.

    A checklist ends at the next same-or-higher heading, or at the ``:::`` that
    closes an admonition. Taking the rest of the file instead would let a status
    named anywhere below — in an unrelated FAQ answer, say — satisfy the
    assertion, which is how a mirror test quietly stops mirroring.
    """
    start = text.find(anchor)
    assert start != -1, f"anchor {anchor!r} is gone — the section was renamed or removed"
    body = text[start + len(anchor) :]
    end = re.search(r"^(#{1,2} |:::\s*$)", body, re.MULTILINE)
    return body[: end.start()] if end else body


@pytest.mark.parametrize(("path", "anchor"), _CHECKLISTS)
def test_the_silence_checklist_names_every_suppressing_status(path: Path, anchor: str) -> None:
    section = _section(path.read_text(encoding="utf-8"), anchor).lower()
    # The tuple holds column values (``false_positive``); the prose spells them
    # for humans (``false positive``). Accept either, since the point is that the
    # reader can recognise the badge they clicked, not that the docs quote an
    # enum.
    missing = [
        status
        for status in _SUPPRESSING_INBOX_STATUSES
        if status not in section and status.replace("_", " ") not in section
    ]
    assert not missing, (
        f"{path.relative_to(_REPO_ROOT)} section {anchor!r} does not mention "
        f"{missing} — every status in _SUPPRESSING_INBOX_STATUSES stops deliveries, "
        "so a reader whose alerts are silenced by one cannot diagnose it here "
        ". Add it, or explain why it belongs somewhere else."
    )


def test_the_canonical_explanation_covers_the_same_statuses() -> None:
    """The section both checklists send people to must cover all four too.

    Each checklist links to ``alerting.md#silencing-an-incident`` for "which
    decision did I actually want". A complete checklist pointing at an incomplete
    explanation is the same defect one hop further away.
    """
    section = _section(
        (_DOCS / "alerting.md").read_text(encoding="utf-8"),
        "### Silencing an incident",
    ).lower()
    missing = [
        status
        for status in _SUPPRESSING_INBOX_STATUSES
        if status not in section and status.replace("_", " ") not in section
    ]
    assert not missing, (
        f"website/docs/use/alerting.md 'Silencing an incident' omits {missing}, "
        "which is the section the troubleshooting checklists link to for the full "
        "answer."
    )
