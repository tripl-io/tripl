"""The notice generator indexes the vendored /docs and /redoc bundles.

``tripl/api_docs/static/third-party.json`` names each bundle and its license
files; the script copies those into ``api-docs/<name>-<version>/`` and lists
them in the README.
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


def test_finds_the_installed_packages_manifest(tpl: ModuleType) -> None:
    static = tpl.api_docs_dir()
    assert static is not None
    assert (static / "third-party.json").is_file()


def test_copies_each_bundles_license_files_and_lists_them(tpl: ModuleType, tmp_path: Path) -> None:
    static = tpl.api_docs_dir()
    manifest = json.loads((static / "third-party.json").read_text(encoding="utf-8"))
    entries = tpl.collect_api_docs(tmp_path, static)

    assert {e.name for e in entries} == {p["name"] for p in manifest["packages"]}
    for package in manifest["packages"]:
        entry = next(e for e in entries if e.name == package["name"])
        assert entry.version == package["version"]
        assert entry.license == package["license"]
        folder = f"api-docs/{package['name']}-{package['version']}"
        assert entry.files == tuple(f"{folder}/{name}" for name in package["license_files"])
        for rel in entry.files:
            copied = tmp_path / rel
            assert copied.read_bytes() == (static / copied.name).read_bytes()

    tpl.write_readme(tmp_path, [], [], 0, entries)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "## Bundled API reference (`/docs`, `/redoc`)" in readme
    for entry in entries:
        assert f"| {entry.name} | {entry.version} | {entry.license} |" in readme


def test_no_manifest_means_no_section(tpl: ModuleType, tmp_path: Path) -> None:
    assert tpl.collect_api_docs(tmp_path, None) == []
    tpl.write_readme(tmp_path, [], [], 0, [])
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "Bundled API reference" not in readme
    assert not (tmp_path / "api-docs").exists()
