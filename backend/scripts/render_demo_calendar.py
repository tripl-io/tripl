"""Rewrite the demo calendar tables in website/docs/use/demo-workspace.md.

Run from backend/ after changing ``core/demo_warehouse/calendar.py``:

    uv run python scripts/render_demo_calendar.py

``tests/test_demo_calendar_doc.py`` fails until the page matches.
"""

from __future__ import annotations

from pathlib import Path

from tripl.core.demo_warehouse.calendar_doc import replace_block
from tripl.services.demo.builders.warehouse import DISTRIBUTION_DRIFT_FIELDS

PAGE = Path(__file__).resolve().parents[2] / "website" / "docs" / "use" / "demo-workspace.md"


def main() -> None:
    page = PAGE.read_text(encoding="utf-8")
    updated = replace_block(page, DISTRIBUTION_DRIFT_FIELDS)
    if updated != page:
        PAGE.write_text(updated, encoding="utf-8")
        print(f"updated {PAGE}")
    else:
        print(f"{PAGE} is up to date")


if __name__ == "__main__":
    main()
