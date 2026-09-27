"""``tripl codegen`` and ``tripl export`` end to end: config, the export read, files, exit codes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from tripl_cli.cli import main
from tripl_cli.commands.export import safe_name, schema_files

from .conftest import API_BASE, FakeInstance, make_branch

FIXTURES = Path(__file__).parent / "codegen"
EXPORT_URL = f"{API_BASE}/projects/demo/plan/export"
MODEL = json.loads((FIXTURES / "model.json").read_text(encoding="utf-8"))
REVISION = MODEL["revision"]
PLAN_HASH = MODEL["plan_hash"]

BUNDLE = {
    "format": "jsonschema",
    "revision": REVISION,
    "branch": "main",
    "schemas": {
        "se/home:open:card": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["category"],
            "properties": {"category": {"const": "home"}},
        },
        "sd/iglu:com.example/checkout_started/jsonschema/1-0-0": {"type": "object"},
        "legacy/../../escape": {"type": "object"},
        "legacy/..//escape": {"type": "object"},
    },
}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".tripl").mkdir(parents=True)
    (root / ".git").mkdir()
    shutil.copy(FIXTURES / "check.yml", root / ".tripl" / "check.yml")
    (root / "model.json").write_text(json.dumps(MODEL), encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def _exporter(tripl_api: FakeInstance) -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("format") == "jsonschema":
            return httpx.Response(200, json=BUNDLE)
        # The route names the branch it read, "main" for main.
        branch = "checkout-redesign" if "branch" in request.url.params else "main"
        return httpx.Response(200, json=dict(MODEL, branch=branch))

    return tripl_api.handler(EXPORT_URL, respond)


# --- codegen: offline, from a saved model ----------------------------------------------
def test_codegen_writes_every_file_then_check_is_clean(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["codegen", "--model", "model.json"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("tripl codegen - offline (from --model)\n")
    assert f"15 files from the plan (revision {REVISION} of main): 15 created" in out
    assert "  created    generated/swift/AppEvents.swift" in out
    golden = FIXTURES / "golden" / "ts" / "legacyTracking.ts"
    assert (repo / "generated/ts/legacyTracking.ts").read_bytes() == golden.read_bytes()

    assert main(["codegen", "--model", "model.json", "--check"]) == 0
    assert "15 generated files are in sync with the plan" in capsys.readouterr().out


def test_check_exits_1_on_a_changed_a_missing_and_a_stale_file_and_writes_nothing(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["codegen", "--model", "model.json"]) == 0
    generated = repo / "generated"
    edited = generated / "ts" / "appEvents.ts"
    edited.write_text(edited.read_text(encoding="utf-8") + "// hand edit\n", encoding="utf-8")
    (generated / "kotlin" / "AppEvents.kt").unlink()
    stale = generated / "swift" / "OldTracking.swift"
    shutil.copy(generated / "swift" / "AppEvents.swift", stale)
    helper = generated / "swift" / "Helpers.swift"
    helper.write_text("// written by hand\n", encoding="utf-8")
    capsys.readouterr()

    assert main(["codegen", "--model", "model.json", "--check", "--json"]) == 1
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["command"] == "codegen"
    assert document["check"] is True and document["exit_code"] == 1
    assert document["revision"] == REVISION
    drift = {
        row["path"]: row["status"] for row in document["files"] if row["status"] != "unchanged"
    }
    assert drift == {
        "generated/ts/appEvents.ts": "changed",
        "generated/kotlin/AppEvents.kt": "created",
        "generated/swift/OldTracking.swift": "stale",
    }
    assert "3 of 16 generated files are out of date" in captured.err
    # --check wrote nothing and removed nothing.
    assert "// hand edit" in edited.read_text(encoding="utf-8")
    assert stale.exists() and not (generated / "kotlin" / "AppEvents.kt").exists()

    assert main(["codegen", "--model", "model.json"]) == 0
    assert not stale.exists(), "a stale generated file is removed on a write"
    assert helper.exists(), "a file without the generated header is never touched"
    assert main(["codegen", "--model", "model.json", "--check"]) == 0


def test_stale_removal_spares_other_projects_and_languages_the_run_skips(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["codegen", "--model", "model.json"]) == 0
    generated = repo / "generated"
    # Another project's generated file sharing the output directory.
    other = generated / "swift" / "OtherTracking.swift"
    other.write_text(
        (generated / "swift" / "AppEvents.swift")
        .read_text(encoding="utf-8")
        .replace("// Plan: project demo, ", "// Plan: project other, ", 1),
        encoding="utf-8",
    )
    # A generated file of this project, of a language this run writes elsewhere.
    kotlin_in_swift = generated / "swift" / "Stray.kt"
    shutil.copy(generated / "kotlin" / "AppEvents.kt", kotlin_in_swift)
    # A generated file of this project that this run no longer produces: stale.
    stale = generated / "swift" / "OldTracking.swift"
    shutil.copy(generated / "swift" / "AppEvents.swift", stale)
    capsys.readouterr()

    assert main(["codegen", "--model", "model.json"]) == 0
    assert not stale.exists()
    assert other.exists(), "another project's generated file is never removed"
    assert kotlin_in_swift.exists(), "only the languages written into a directory are swept"


def test_a_new_revision_with_the_same_content_is_not_drift(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["codegen", "--model", "model.json"]) == 0
    moved = dict(MODEL, revision="00000000-0000-4000-8000-000000000001")
    (repo / "model.json").write_text(json.dumps(moved), encoding="utf-8")
    assert main(["codegen", "--model", "model.json", "--check"]) == 0
    # ...while a new content hash is.
    changed = dict(moved, plan_hash="ffffffffffff")
    (repo / "model.json").write_text(json.dumps(changed), encoding="utf-8")
    assert main(["codegen", "--model", "model.json", "--check"]) == 1


def test_lang_limits_the_run_to_one_language(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # One language: --out is the directory itself, as in `--lang swift --out App/Generated`.
    assert main(["codegen", "--model", "model.json", "--lang", "ts", "--out", "web"]) == 0
    written = sorted(path.relative_to(repo).as_posix() for path in (repo / "web").rglob("*"))
    assert written == [
        "web/appEvents.ts",
        "web/legacyTracking.ts",
        "web/screenTracking.ts",
        "web/sdTracking.ts",
        "web/triplTransport.ts",
    ]
    check = ["codegen", "--model", "model.json", "--lang", "ts", "--out", "web", "--check"]
    assert main(check) == 0
    # Several languages: one directory per language under --out.
    assert main(["codegen", "--model", "model.json", "--out", "all"]) == 0
    assert sorted(path.name for path in (repo / "all").iterdir()) == ["kotlin", "swift", "ts"]


def test_codegen_without_a_check_config_is_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main(["codegen", "--model", "x.json"]) == 2
    assert "no check config found" in capsys.readouterr().err


def test_a_model_file_that_is_not_an_export_is_exit_2(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "bundle.json").write_text(json.dumps(BUNDLE), encoding="utf-8")
    assert main(["codegen", "--model", "bundle.json"]) == 2
    assert "is not a codegen_model export" in capsys.readouterr().err


def test_an_event_type_the_plan_lacks_is_exit_1(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    model = dict(MODEL, event_types=[t for t in MODEL["event_types"] if t["name"] != "sd"])
    (repo / "model.json").write_text(json.dumps(model), encoding="utf-8")
    assert main(["codegen", "--model", "model.json"]) == 1
    assert "the plan has no event type named 'sd'" in capsys.readouterr().err


# --- codegen: from the instance --------------------------------------------------------
def test_codegen_reads_the_codegen_model_export(
    repo: Path,
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = _exporter(tripl_api)
    assert main(["codegen", "--check"]) == 1  # nothing generated yet: all 15 are missing
    request = route.calls.last.request
    assert request.url.params["format"] == "codegen_model"
    assert "branch" not in request.url.params
    assert capsys.readouterr().out.startswith("tripl codegen - http://tripl.test")


def test_codegen_resolves_a_branch_and_names_it_in_the_header(
    repo: Path,
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    tripl_api.branches("demo", [make_branch()])
    route = _exporter(tripl_api)
    assert main(["codegen", "--branch", "checkout-redesign"]) == 0
    assert route.calls.last.request.url.params["branch"] == "b-9f21"
    header = (repo / "generated/swift/AppEvents.swift").read_text(encoding="utf-8")
    assert f"// Plan: project demo, branch checkout-redesign, plan {PLAN_HASH}." in header


def test_model_and_branch_together_are_refused(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["codegen", "--model", "model.json", "--branch", "x"]) == 2
    assert "drop --branch" in capsys.readouterr().err


# --- export ------------------------------------------------------------------------------
def test_safe_names_never_leave_the_output_directory() -> None:
    assert safe_name("home:open:card") == "home_open_card"
    assert safe_name("../../escape") == "escape"
    assert safe_name("..") == "_"
    assert safe_name("") == "_"
    files = schema_files(BUNDLE, Path("out"))
    names = sorted(path.as_posix() for path in files)
    assert names == [
        "out/legacy/escape-2.schema.json",
        "out/legacy/escape.schema.json",
        "out/sd/iglu_com.example_checkout_started_jsonschema_1-0-0.schema.json",
        "out/se/home_open_card.schema.json",
    ]


def test_export_writes_the_bundle_and_one_schema_per_event(
    repo: Path,
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = _exporter(tripl_api)
    assert main(["export", "--format", "jsonschema", "--out", "schemas", "--json"]) == 0
    assert route.calls.last.request.url.params["format"] == "jsonschema"
    document = json.loads(capsys.readouterr().out)
    assert document["command"] == "export"
    assert document["format"] == "jsonschema"
    assert document["schemas"] == 4
    assert document["files"][0] == "schemas/bundle.json"
    bundle = json.loads((repo / "schemas/bundle.json").read_text(encoding="utf-8"))
    assert bundle == BUNDLE
    schema = json.loads((repo / "schemas/se/home_open_card.schema.json").read_text("utf-8"))
    assert schema["required"] == ["category"]
    assert not (repo.parent / "escape.schema.json").exists()


def test_export_prints_to_stdout_without_out(
    repo: Path,
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _exporter(tripl_api)
    assert main(["export", "--format", "codegen_model"]) == 0
    assert json.loads(capsys.readouterr().out) == MODEL


def test_export_json_needs_an_output_directory(
    repo: Path, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["export", "--format", "jsonschema", "--json"]) == 2
    assert "add --out DIR" in capsys.readouterr().err


def test_a_saved_codegen_model_export_feeds_codegen(
    repo: Path,
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _exporter(tripl_api)
    assert main(["export", "--format", "codegen_model", "--out", "saved"]) == 0
    capsys.readouterr()
    assert main(["codegen", "--model", "saved/codegen_model.json"]) == 0
    assert (repo / "generated/kotlin/SdTracking.kt").exists()
