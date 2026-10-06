"""The image's third-party notice generator (backend/scripts/third_party_licenses.py).

Runs it against this test environment's own venv: the image runs it the same
way, inside the image, so what it finds here is what it finds there, minus the
dev dependencies.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    script = Path(__file__).resolve().parents[3] / "scripts" / "third_party_licenses.py"
    spec = importlib.util.spec_from_file_location("_third_party_licenses", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tpl() -> ModuleType:
    return _load()


def test_copies_psycopg_lgpl_text_and_says_it_can_be_replaced(
    tpl: ModuleType, tmp_path: Path
) -> None:
    entries = tpl.collect_python(tmp_path)
    psycopg = next(e for e in entries if e.name == "psycopg")
    assert "LGPL" in psycopg.license
    assert psycopg.files, "psycopg's license file was not found"
    text = (tmp_path / psycopg.files[0]).read_text(encoding="utf-8")
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in text

    tpl.write_readme(tmp_path, entries, [], 0)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "LGPL-licensed libraries (psycopg" in readme
    assert f"`{psycopg.files[0]}`" in readme


def test_leaves_out_our_own_distribution(tpl: ModuleType, tmp_path: Path) -> None:
    names = {e.name for e in tpl.collect_python(tmp_path)}
    assert "tripl-server" not in names
    assert "fastapi" in names


def test_indexes_the_frontend_list_when_present(tpl: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "index.json").write_text(
        json.dumps(
            [
                {
                    "name": "react",
                    "version": "19.3.0",
                    "license": "MIT",
                    "files": ["frontend/react@19.3.0/LICENSE"],
                },
                {"name": "no-file", "version": "1.0.0", "license": "MIT", "files": []},
            ]
        ),
        encoding="utf-8",
    )
    tpl.write_readme(tmp_path, [], tpl._frontend_entries(tmp_path), 0)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "## Web application" in readme
    assert "| react | 19.3.0 | MIT | `frontend/react@19.3.0/LICENSE` |" in readme
    assert "| no-file | 1.0.0 | MIT | none shipped |" in readme
    # No LGPL package among the inputs, so no LGPL paragraph.
    assert "LGPL" not in readme


def test_a_rerun_replaces_the_python_list(
    tpl: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / "python" / "gone-0.0.1"
    stale.mkdir(parents=True)
    (stale / "LICENSE").write_text("old", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["third_party_licenses.py", "--out", str(tmp_path), "--no-os"])
    assert tpl.main() == 0
    assert not stale.exists()
    assert (tmp_path / "README.md").is_file()
