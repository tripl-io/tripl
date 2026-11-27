"""The captured-event samples in the docs are lines ``--payloads`` accepts.

The how-to page on recording events in UI tests, and the payload mode section of
the ``tripl check`` guide, show what a recorder should write. Every one of
their NDJSON samples is parsed here by the parser ``--payloads`` runs, so a
sample cannot drift into a line the check refuses. It proves the documented
lines parse, not that a recorder snippet writes them: the Kotlin, Swift and
TypeScript snippets are examples to adapt and are not compiled here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_contract import REPO_ROOT
from tripl_cli.check import payloads
from tripl_cli.errors import TriplConfigError

HOW_TO = REPO_ROOT / "website" / "docs" / "how-to" / "record-events-in-ui-tests.md"
GUIDE = REPO_ROOT / "website" / "docs" / "integrate" / "tripl-check.md"
FENCE = re.compile(r"^```json\n(.*?)^```$", re.M | re.S)
KEY_ROW = re.compile(r"^\| `([a-z_]+)` \|", re.M)


def _section(document: Path, heading: str) -> str:
    """The text from ``heading`` up to the next heading of its level or above."""
    text = document.read_text(encoding="utf-8")
    start = text.index(heading)
    level = heading.split(" ", 1)[0]
    following = re.compile(rf"^#{{1,{len(level)}}} ", re.M).search(text, start + len(heading))
    return text[start : following.start() if following else len(text)]


def _samples() -> list[tuple[str, str]]:
    """Every NDJSON sample: each json block of the how-to, and the guide's in its modes section."""
    found = [
        (f"{HOW_TO.name} sample {number}", block)
        for number, block in enumerate(FENCE.findall(HOW_TO.read_text(encoding="utf-8")), 1)
    ]
    modes = FENCE.findall(_section(GUIDE, "## Static and payload modes {#modes}"))
    found += [
        (f"{GUIDE.name}#modes sample {number}", block) for number, block in enumerate(modes, 1)
    ]
    return found


SAMPLES = _samples()


def test_the_docs_have_the_samples_this_file_checks() -> None:
    # The format, the Snowplow mapping, and the guide's example.
    assert len(SAMPLES) == 3, [label for label, _ in SAMPLES]


@pytest.mark.parametrize(("label", "sample"), SAMPLES, ids=[label for label, _ in SAMPLES])
def test_every_documented_line_is_one_complete_event_payloads_accepts(
    label: str, sample: str
) -> None:
    lines = [line for line in sample.splitlines() if line.strip()]
    items = payloads.parse(sample, label)
    assert len(items) == len(lines) > 1
    for line, item in zip(lines, items, strict=True):
        assert item.complete
        assert item.notes == ()  # within the validator's limits, nothing cut
        assert item.event_type is not None or item.name is not None
        # One object per line, as a recorder writes it: no line is a fragment.
        assert isinstance(json.loads(line), dict)


@pytest.mark.parametrize(("label", "sample"), SAMPLES, ids=[label for label, _ in SAMPLES])
def test_ref_is_a_label_for_the_reader_and_never_changes_an_event(label: str, sample: str) -> None:
    without = "\n".join(
        json.dumps({key: value for key, value in json.loads(line).items() if key != "ref"})
        for line in sample.splitlines()
    )
    assert payloads.parse(without, label) == payloads.parse(sample, label)


@pytest.mark.parametrize("document", [HOW_TO, GUIDE], ids=["how-to", "guide"])
def test_the_key_tables_name_every_key_a_line_may_hold(document: Path) -> None:
    heading = "## 1. The file to write {#format}" if document == HOW_TO else "## Static and payload"
    table = _section(document, heading)
    assert set(KEY_ROW.findall(table)) == set(payloads._KEYS)


def test_a_line_with_another_key_names_the_keys_a_line_may_hold() -> None:
    with pytest.raises(TriplConfigError) as refused:
        payloads.parse('{"name": "a", "screen": "home"}\n{"name": "b"}\n', "events.ndjson")
    message = str(refused.value)
    assert "line 1 has unknown key(s) screen" in message
    assert all(key in message for key in payloads._KEYS)


# --- the recorder snippets ------------------------------------------------------------------------
# They are examples to adapt and are not compiled here, so these tests hold the
# few lines whose mistake would stop the check or crash the app under test.
def _code_block(language: str, marker: str) -> str:
    blocks = re.findall(
        rf"^```{language}\n(.*?)^```$", HOW_TO.read_text(encoding="utf-8"), re.M | re.S
    )
    [block] = [block for block in blocks if marker in block]
    return block


def test_the_snowplow_recorder_writes_only_the_events_it_maps() -> None:
    # toLine gives nothing for a page view or a page ping. Handing that to the
    # Playwright writer would write a line holding only ref, which --payloads
    # refuses (exit 2) on the first page view of the run.
    block = _code_block("typescript", "afterTrack")
    assert "toLine(payload));" not in block
    assert re.search(r"const line = toLine\(payload\);\s*if \(line\) ", block)
    with pytest.raises(TriplConfigError, match="names neither an event_type nor a name"):
        payloads.parse('{"ref": "Checkout > pays"}\n', "events.ndjson")


def test_the_swift_recorder_checks_a_value_is_json_before_serializing_it() -> None:
    # JSONSerialization raises an Objective-C exception, which try? cannot
    # catch, for a value that is not JSON (a Date, a URL): the app would crash.
    block = _code_block("swift", "RecordingTracker")
    check = block.index("JSONSerialization.isValidJSONObject(")
    assert check < block.index("JSONSerialization.data(")


def test_a_run_that_sends_nothing_must_still_leave_a_file(tmp_path: Path) -> None:
    # --allow-empty accepts an empty file, never a missing one, so the page has
    # the UI-test job create the file before the tests run.
    missing = str(tmp_path / "events.ndjson")
    with pytest.raises(TriplConfigError, match="does not exist"):
        payloads.load(missing, allow_empty=True)
    (tmp_path / "events.ndjson").write_text("", encoding="utf-8")
    assert payloads.load(missing, allow_empty=True) == []
    create = ": > build/events.ndjson"
    for marker in ("ci/gitlab/tripl-check.yml", "tripl-check-action"):
        recipe = _code_block("yaml", marker)
        assert recipe.index(create) < recipe.index("./run-ui-tests.sh"), marker
    assert "`--allow-empty` accepts an empty file, not a missing one" in HOW_TO.read_text(
        encoding="utf-8"
    )
