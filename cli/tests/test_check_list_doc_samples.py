"""The ``check --list`` and ``--print-preset`` samples in the docs are real output.

The text sample and JSON example in ``website/docs/run/cli.md`` and the
``--print-preset`` header in the guide were kept in step with the CLI by hand,
and the header drifted once (#461). Here the sample repository the docs describe
is rebuilt, the commands are run against it, and their output is compared with
the docs byte for byte, apart from what differs per run (``generated_at``,
``duration_ms``, the tool version and the absolute paths).
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from typing import Any

import pytest

from tests.test_contract import DOCS_PATH, REPO_ROOT
from tripl_cli.cli import main

GUIDE_PATH = REPO_ROOT / "website" / "docs" / "integrate" / "tripl-check.md"

# The repository the samples describe: two Kotlin files, four matched calls.
SAMPLE_CONFIG = """
project: shop
sources: ["app/**"]
event_types:
  event:
    calls:
      - {function: "analytics.logEvent", positional: [name, properties]}
"""

SAMPLE_SOURCES = {
    "app/Tracking.kt": """
        package app

        import com.example.analytics.Analytics

        // Every screen reports through here.
        object Tracking {
            private val analytics = Analytics()

            fun track(event: String, params: Map<String, Any>) {
                analytics.logEvent(event, params)
            }
        }
    """,
    "app/checkout/CheckoutScreen.kt": """
        package app.checkout

        class CheckoutScreen {
            fun show(step: String) {
                analytics.logEvent("checkout_started", mapOf("step" to step, "cart" to "main"))
                tracking.track("checkout_viewed", emptyMap())
            }

            fun promo(promo: String) {
                analytics.logEvent("promo_%s_shown".format(promo))
            }

            fun onAction(action: Action) {
                analytics.logEvent(action.event)
            }

            fun relayStarted() {
                relay(Events.CHECKOUT_STARTED)
            }
        }

        object Events {
            const val CHECKOUT_STARTED = "checkout_started"
        }
    """,
}


def _fenced_block(document: Path, anchor: str, language: str) -> str:
    """The first ``language`` fenced block after the line ``anchor``."""
    text = document.read_text(encoding="utf-8")
    start = text.index(anchor)
    match = re.compile(rf"^```{language}\n(.*?)^```$", re.M | re.S).search(text, start)
    assert match is not None, f"no ```{language} block after {anchor!r} in {document.name}"
    return match.group(1)


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "shop"
    for relative, body in {".tripl/check.yml": SAMPLE_CONFIG, **SAMPLE_SOURCES}.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    return root.resolve()


def test_the_list_text_sample_is_what_check_list_prints(
    shop: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    documented = _fenced_block(DOCS_PATH, "### List what the scan reads", "text")

    assert main(["check", "--list"]) == 0
    printed = capsys.readouterr().out.replace(str(shop), "/home/me/shop")

    assert printed == documented


def _without_run_details(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in document.items()
        if key not in ("generated_at", "duration_ms", "tool_version")
    }


def test_the_list_json_example_is_check_list_json_trimmed_to_one_entry_per_list(
    shop: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    documented = json.loads(_fenced_block(DOCS_PATH, "### `check --list` document", "json"))

    assert main(["check", "--list", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out.replace(str(shop), "/home/ci/shop"))
    # The docs show one entry of each list, the first.
    trimmed = {
        **printed,
        **{key: printed[key][:1] for key in ("calls", "forwarders", "unmatched")},
    }

    assert _without_run_details(trimmed) == _without_run_details(documented)
    assert list(printed) == list(documented), "the docs list every key, in order"


def test_the_print_preset_header_sample_is_what_print_preset_prints(
    capsys: pytest.CaptureFixture[str],
) -> None:
    documented = _fenced_block(GUIDE_PATH, "`tripl check --print-preset segment_track`", "text")

    # An argparse action, like --version: it prints and exits during parsing.
    with pytest.raises(SystemExit) as exited:
        main(["check", "--print-preset", "segment_track"])
    assert exited.value.code == 0
    printed = capsys.readouterr().out

    header = "".join(printed.splitlines(keepends=True)[: documented.count("\n")])
    assert header == documented
