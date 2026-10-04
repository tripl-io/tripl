"""Guard against drift between this backend's scheduler and the CLI's copy of it.

``tripl doctor`` reports the metrics scheduler's retry backoff and judges scan
staleness, and to do that ``cli/`` MIRRORS five facts declared here. The mirror is
deliberate — the cli package has no backend dependency, because it installs on an
operator's laptop — but until this file nothing tied the copies together: the
CLI's own pinning test compares its formula to literals inside its own package,
so it cannot see the backend at all, and its CI job runs from ``cli/``. A backend
change therefore left every CLI test green while ``deferred_by_seconds_estimate``,
the published backoff table in ``website/docs/run/cli.md`` and the (since removed)
demo staleness allowance all quietly became wrong.

The guard lives HERE rather than in ``cli/tests/`` because this is the suite whose
CI job has the whole repository checked out. It reads the two CLI modules as text
via ``ast`` — importing them would need the cli package installed, and this test
must cost the backend nothing. Same shape as ``test_alembic_revisions.py``: cheap,
structural, and it names both sides of the mismatch so the message alone is enough
to fix it.
"""

from __future__ import annotations

import ast
import operator
from pathlib import Path

import pytest

from tripl.core.intervals import INTERVALS
from tripl.worker.tasks.metrics.schedule import (
    FAILURE_BACKOFF_AFTER,
    FAILURE_BACKOFF_CEILING,
    FAILURE_BACKOFF_MAX_INTERVALS,
)
from tripl.worker.tasks.metrics.tasks import METRICS_COLLECTION_MODE

# tests -> tripl -> src -> backend -> repository root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_PACKAGE = _REPO_ROOT / "cli" / "src" / "tripl_cli"
_DIAGNOSTICS = _PACKAGE / "diagnostics"
_SCAN_CHECKS = _DIAGNOSTICS / "scan_checks.py"
# At the package root, not under diagnostics/: the snapshot dataclasses serve
# every command including the verdict-free ones, so they were moved out.
_MODEL = _PACKAGE / "model.py"

_SCHEDULE = "backend/src/tripl/worker/tasks/metrics/schedule.py"
_TASKS = "backend/src/tripl/worker/tasks/metrics/tasks.py"
_INTERVALS = "backend/src/tripl/core/intervals.py"

# Every mirrored fact: where the CLI keeps it, and what this backend says it is.
# ``origin`` is quoted verbatim in the failure message, so whoever breaks this can
# open both files without reading the test.
_MIRRORED: tuple[tuple[Path, str, object, str], ...] = (
    (
        _SCAN_CHECKS,
        "FAILURE_BACKOFF_AFTER",
        FAILURE_BACKOFF_AFTER,
        f"{_SCHEDULE}::FAILURE_BACKOFF_AFTER",
    ),
    (
        _SCAN_CHECKS,
        "FAILURE_BACKOFF_MAX_INTERVALS",
        FAILURE_BACKOFF_MAX_INTERVALS,
        f"{_SCHEDULE}::FAILURE_BACKOFF_MAX_INTERVALS",
    ),
    (
        _SCAN_CHECKS,
        "FAILURE_BACKOFF_CEILING_SECONDS",
        int(FAILURE_BACKOFF_CEILING.total_seconds()),
        f"{_SCHEDULE}::FAILURE_BACKOFF_CEILING (as whole seconds)",
    ),
    (
        _SCAN_CHECKS,
        "DISPATCHER_MODE",
        METRICS_COLLECTION_MODE,
        f"{_TASKS}::METRICS_COLLECTION_MODE",
    ),
    (
        _MODEL,
        "INTERVAL_SECONDS",
        {code: int(spec.delta.total_seconds()) for code, spec in INTERVALS.items()},
        f"{_INTERVALS}::INTERVALS (each spec's delta, as whole seconds)",
    ),
)

pytestmark = pytest.mark.skipif(
    not _DIAGNOSTICS.is_dir(),
    reason=(
        f"cli/ is not checked out at {_DIAGNOSTICS}, so there is no mirror to check; "
        "the backend package stays independently testable"
    ),
)

# The CLI writes its durations as the arithmetic it means (``24 * 3600``), which
# ``ast.literal_eval`` rejects. Folding these two operators keeps the guard from
# forcing the mirror to spell out 86400.
_FOLDABLE = {ast.Mult: operator.mul, ast.Add: operator.add}


def _value(node: ast.expr) -> object:
    """``ast.literal_eval`` plus the arithmetic above, applied recursively."""
    if isinstance(node, ast.BinOp) and type(node.op) in _FOLDABLE:
        return _FOLDABLE[type(node.op)](_value(node.left), _value(node.right))
    if isinstance(node, ast.Dict):
        return {
            _value(key): _value(value)
            for key, value in zip(node.keys, node.values, strict=True)
            if key is not None
        }
    return ast.literal_eval(node)


def _assignments(path: Path) -> dict[str, ast.expr]:
    """Every module-level ``NAME = <expr>`` in a file, left unevaluated.

    Unevaluated because only the six names below have to be readable: a module
    holds plenty of assignments this evaluator cannot fold, and none of them is
    this test's business.
    """
    found: dict[str, ast.expr] = {}
    if not path.is_file():
        # A moved mirror is exactly what this test exists to catch, so it must
        # arrive as the message below rather than as a pathlib traceback. It did
        # arrive as a traceback once (model.py was moved to the package
        # root), and "FileNotFoundError in _assignments" says nothing about
        # which constant stopped being checked.
        return found
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if node.value is not None:
            found.update(dict.fromkeys(names, node.value))
    return found


@pytest.mark.parametrize(
    ("path", "name", "backend_value", "origin"),
    _MIRRORED,
    ids=[name for _, name, _, _ in _MIRRORED],
)
def test_cli_mirror_matches_backend(
    path: Path, name: str, backend_value: object, origin: str
) -> None:
    relative = path.relative_to(_REPO_ROOT)
    node = _assignments(path).get(name)

    # A rename must fail as loudly as a changed value: a mirror this test can no
    # longer find is a mirror nothing is checking.
    assert node is not None, (
        f"{relative} has no module-level {name}, but it is meant to mirror {origin} "
        f"(= {backend_value!r}). Restore the name, or drop its row from _MIRRORED in "
        f"{Path(__file__).name} if doctor genuinely stopped depending on this backend fact."
    )

    try:
        cli_value = _value(node)
    except (ValueError, TypeError) as exc:
        pytest.fail(
            f"{relative}::{name} is no longer a plain literal this guard can read "
            f"({exc}), so its agreement with {origin} (= {backend_value!r}) cannot be "
            f"checked. Keep it a literal, or widen _value() in {Path(__file__).name}."
        )

    assert cli_value == backend_value, (
        f"tripl doctor mirrors a scheduler fact that this backend has changed:\n"
        f"  {relative}::{name} = {cli_value!r}\n"
        f"  {origin} = {backend_value!r}\n"
        "Update the CLI copy to match. doctor's backoff estimate "
        "and the backoff table published in website/docs/run/cli.md are all "
        "computed from that copy, so leaving it stale makes doctor confidently wrong."
    )
