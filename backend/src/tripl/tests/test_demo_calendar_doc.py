"""The demo workspace page shows the calendar the warehouse actually runs.

The tables are rendered from the calendar's constants; when they change, run
``uv run python scripts/render_demo_calendar.py`` from backend/.
"""

from __future__ import annotations

from pathlib import Path

from tripl.core.demo_warehouse import calendar
from tripl.core.demo_warehouse.calendar_doc import (
    BEGIN_MARKER,
    END_MARKER,
    calendar_markdown,
    replace_block,
)
from tripl.services.demo.builders.warehouse import DISTRIBUTION_DRIFT_FIELDS

PAGE = Path(__file__).resolve().parents[4] / "website" / "docs" / "use" / "demo-workspace.md"


def test_the_page_carries_the_rendered_calendar() -> None:
    page = PAGE.read_text(encoding="utf-8")

    assert page.count(BEGIN_MARKER) == 1
    assert page.count(END_MARKER) == 1
    assert replace_block(page, DISTRIBUTION_DRIFT_FIELDS) == page, (
        "website/docs/use/demo-workspace.md is out of date: run "
        "`uv run python scripts/render_demo_calendar.py` from backend/"
    )


def test_every_incident_target_and_regression_is_listed() -> None:
    block = calendar_markdown(DISTRIBUTION_DRIFT_FIELDS)

    for _kind, _scope, targets, *_rest in calendar._INCIDENT_KINDS:
        for target in targets:
            assert f"`{target}`" in block
    for event, _kind, _kept in calendar.REGRESSION_ROTATION:
        assert f"`{event}`" in block
    for name, *_schedule in calendar._UNPLANNED_SCHEDULE:
        assert f"`{name}`" in block


def test_a_mix_shift_is_called_drift_only_on_a_watched_field() -> None:
    watched = calendar_markdown(("platform", "country"))
    unwatched = calendar_markdown(())

    assert "distribution drift" in watched
    assert "distribution drift" not in unwatched
