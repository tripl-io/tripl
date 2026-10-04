"""A name-format failure must reach the operator with its reason intact.

``_apply_name_format`` raised a bare ``ValueError``, so the worker's
sanitiser collapsed the one self-diagnosing line ("references unknown keys:
action") into "Scan failed due to an internal error." for four days.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tripl.core.analyzers.event_generator import (
    _NAME_FORMAT_ERROR_BUDGET,
    _apply_name_format,
)
from tripl.core.name_template import NameFormatError
from tripl.worker.tasks._errors import _MAX_CURATED_LEN, user_facing_error

GENERIC = "Scan failed due to an internal error."


def test_apply_name_format_raises_name_format_error_naming_the_missing_key() -> None:
    """The prefix is load-bearing: frontend/src/lib/scanError.ts only passes a
    backend message through verbatim when it starts with "scan failed". Drop it
    and the operator reads a bare "Scan failed." again."""
    with pytest.raises(NameFormatError) as excinfo:
        _apply_name_format("{action}", {"screen_name": "home"})

    message = str(excinfo.value)
    assert message.startswith("Scan failed")
    assert "action" in message
    assert "screen_name" in message


def test_name_format_error_is_a_value_error() -> None:
    """Subclassing ValueError keeps any existing ``except ValueError`` working."""
    assert issubclass(NameFormatError, ValueError)


def test_user_facing_error_surfaces_a_name_format_error_verbatim() -> None:
    exc = NameFormatError("Scan failed: the event name format references unknown keys: action.")

    assert user_facing_error(exc) == str(exc)
    assert user_facing_error(exc) != GENERIC


def test_user_facing_error_still_sanitises_a_plain_value_error() -> None:
    """The widening is type-based, not text-based: same text, bare ValueError,
    still collapses to the generic summary."""
    exc = ValueError("Scan failed: the event name format references unknown keys: action.")

    assert user_facing_error(exc) == GENERIC


def test_available_keys_are_capped_so_the_missing_key_survives_truncation() -> None:
    """``user_facing_error`` truncates a curated message from the RIGHT at 500
    chars, and a wide warehouse table supplies hundreds of column names."""
    kwargs = {f"column_{i:03d}": "v" for i in range(300)}

    with pytest.raises(NameFormatError) as excinfo:
        _apply_name_format("{action}", kwargs)

    surfaced = user_facing_error(excinfo.value)
    assert len(surfaced) <= _MAX_CURATED_LEN
    assert "action" in surfaced
    assert surfaced.startswith("Scan failed")
    assert "more" in surfaced


def test_metric_rows_name_format_failure_raises_the_same_curated_error() -> None:
    """The SECOND caller of ``_apply_name_format``, reached from chunk processing.

    Option 2 (a curated core exception) is what makes this true without the
    caller opting in — a try/except wrapper at each call site would have to be
    remembered here too.
    """
    from tripl.worker.tasks.metrics.metric_rows import _build_event_name_from_row

    with pytest.raises(NameFormatError) as excinfo:
        _build_event_name_from_row(
            ["home"],
            {"screen_name": {"is_json": False, "is_low": True}},
            {"screen_name": 0},
            {},
            1,
            [],
            "{action}",
        )

    assert "action" in str(excinfo.value)
    assert str(excinfo.value).startswith("Scan failed")


def _imported_modules(path: Path, package_parts: tuple[str, ...]) -> set[str]:
    """Absolute module names a file imports, relative imports resolved.

    ``package_parts`` is the dotted package the file lives in, which is what a
    ``from ..x import y`` has to be resolved against.
    """
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    modules.add(node.module)
                continue
            base = package_parts[: len(package_parts) - (node.level - 1)]
            modules.add(".".join((*base, *(node.module.split(".") if node.module else ()))))
    return modules


def test_core_does_not_import_worker() -> None:
    """``core`` must never import ``worker`` — that layering is why NameFormatError
    lives in ``core.name_template`` instead of subclassing ``ScanError``.

    Parsed with ``ast``, not grepped, the way ``test_cli_constant_mirror`` reads the
    CLI. A text search fails on a comment or docstring that merely QUOTES an import
    line — and the files this rule governs are exactly the ones whose docstrings now
    have to explain the rule, so the grep version would have started failing on its
    own subject matter.
    """
    tripl = Path(__file__).resolve().parents[1]
    core = tripl / "core"
    offenders = sorted(
        str(path.relative_to(core))
        for path in core.rglob("*.py")
        if any(
            module == "tripl.worker" or module.startswith("tripl.worker.")
            for module in _imported_modules(path, ("tripl", *path.relative_to(tripl).parent.parts))
        )
    )

    assert offenders == [], f"core modules importing worker: {offenders}"


def test_the_layering_check_reads_imports_not_prose(tmp_path: Path) -> None:
    """The grep version's two failure modes, pinned so nobody reverts to it.

    ``core.name_template`` and ``core.analyzers.event_generator`` exist to explain
    WHY core must not import worker, so a docstring quoting the forbidden line is
    the expected state of those files, not a violation. A relative import is the
    opposite error: it is a real violation the ``from tripl.worker`` grep never saw.
    """
    prose = tmp_path / "prose.py"
    prose.write_text(
        '"""Never write ``from tripl.worker.tasks._errors import ScanError`` here."""\n'
        "# import tripl.worker is likewise banned\n"
        "from tripl.core.name_template import NameFormatError\n",
        encoding="utf-8",
    )
    assert not any(
        module.startswith("tripl.worker") for module in _imported_modules(prose, ("tripl", "core"))
    )

    # ``...`` from tripl.core.analyzers is tripl — the same forbidden import,
    # spelled the one way the ``from tripl.worker`` grep could never see.
    relative = tmp_path / "relative.py"
    relative.write_text("from ...worker.tasks._errors import ScanError\n", encoding="utf-8")
    assert "tripl.worker.tasks._errors" in _imported_modules(
        relative, ("tripl", "core", "analyzers")
    )


def test_error_budget_mirrors_the_curated_cap() -> None:
    """``core`` cannot import ``_MAX_CURATED_LEN`` (see the layering test above), so
    it declares its own copy. Nothing else ties the two together."""
    assert _NAME_FORMAT_ERROR_BUDGET == _MAX_CURATED_LEN


def test_a_repeated_placeholder_is_reported_once() -> None:
    """ "{action} / {action}" is ONE broken column. Listing it twice reads as two
    separate problems and pushes the available keys further toward the cap."""
    with pytest.raises(NameFormatError) as excinfo:
        _apply_name_format("{action} / {action} / {screen}", {})

    keys = str(excinfo.value).split("references unknown keys: ")[1].split(".")[0]
    assert keys == "action, screen"


def test_the_and_n_more_tail_survives_the_cap_on_long_column_names() -> None:
    """A COUNT cap is not enough — ten long names still overrun 500 chars, the
    truncation cuts from the right, and the "… and N more" tail goes with it, so
    the operator cannot tell the list was summarised at all."""
    kwargs = {f"analytics_events_wide_column_name_{i:03d}": "v" for i in range(120)}

    with pytest.raises(NameFormatError) as excinfo:
        _apply_name_format("{action}", kwargs)

    surfaced = user_facing_error(excinfo.value)
    assert len(surfaced) <= _MAX_CURATED_LEN
    # Untruncated: the tail is intact rather than sliced off the right edge.
    assert surfaced == str(excinfo.value)
    assert surfaced.endswith(" more")
    assert "action" in surfaced


def test_available_keys_says_so_when_the_row_supplies_none() -> None:
    """ "Available keys: " with nothing after it reads like a formatting bug."""
    with pytest.raises(NameFormatError) as excinfo:
        _apply_name_format("{action}", {})

    assert str(excinfo.value).endswith("Available keys: (none)")
