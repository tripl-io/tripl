"""``tripl docs`` end to end, through ``main([...])`` against a fake instance (F22).

The cases worth the most are the disk ones. ``pull`` writes where the export
says, so a bundle path that climbs out of the folder must write NOTHING, not
one file fewer. ``push`` reads a folder an agent-skill layout fills with
scripts, assets and a ``.git``, and only the Markdown may leave the machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tripl_cli.cli import main

from .conftest import API_BASE, FakeInstance

TREE_URL = f"{API_BASE}/projects/prod/docs"
FILE_URL = f"{API_BASE}/projects/prod/docs/file"
EXPORT_URL = f"{API_BASE}/projects/prod/docs/export"
IMPORT_URL = f"{API_BASE}/projects/prod/docs/import"


class _Stdin:
    def __init__(self, answer: str, *, isatty: bool) -> None:
        self._answer = answer
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty

    def readline(self) -> str:
        return self._answer


def make_doc(path: str, *, scope: str = "project", audience: str = "both") -> dict[str, Any]:
    return {
        "scope": scope,
        "path": path,
        "title": path.rsplit("/", 1)[-1].removesuffix(".md"),
        "description": "",
        "tags": [],
        "audience": audience,
        "revision": 2,
        "size_bytes": 10,
        "updated_at": "2026-09-27T10:00:00Z",
        "updated_by_name": None,
    }


def make_tree(project: list[dict[str, Any]], organization: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "project": {"slug": "prod", "name": "Prod"},
        "organization": {"id": "org-1", "slug": "default", "name": "Default"},
        "project_docs": project,
        "organization_docs": organization,
        "limits": {
            "max_file_bytes": 262144,
            "max_files_per_scope": 5000,
            "max_bundle_files": 2000,
            "max_bundle_bytes": 20971520,
        },
    }


def make_result(**lists: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"scope": "project", "mode": "merge", "dry_run": False}
    for key in ("created", "updated", "unchanged", "deleted"):
        result[key] = lists.get(key, [])
    result["skipped"] = []
    result["errors"] = []
    return result


def _respond(tripl_api: FakeInstance, url: str, payload: Any, *, method: str = "GET") -> None:
    tripl_api.handler(url, lambda request: httpx.Response(200, json=payload), method=method)


def _calls(tripl_api: FakeInstance, method: str) -> list[httpx.Request]:
    return [call.request for call in tripl_api.router.calls if call.request.method == method]


def _body(request: httpx.Request) -> dict[str, Any]:
    payload = json.loads(request.content)
    assert isinstance(payload, dict)
    return payload


def _skill_folder(root: Path) -> Path:
    """The agent-skill layout: SKILL.md, references/, and things that are not notes."""
    (root / "references").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / ".git").mkdir()
    (root / "SKILL.md").write_text("---\nname: checkout\n---\n# Checkout\n", encoding="utf-8")
    (root / "references" / "events.md").write_text("[[event:purchase]]\n", encoding="utf-8")
    (root / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (root / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
    (root / "link.md").symlink_to(root / "SKILL.md")
    return root


# --- ls ---------------------------------------------------------------------


def test_ls_lists_both_scopes_by_default(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = make_tree([make_doc("guides/setup.md")], [make_doc("glossary.md", scope="organization")])
    _respond(tripl_api, TREE_URL, tree)
    assert main(["docs", "ls", "--project", "prod", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["command"] == "docs ls"
    assert document["kind"] == "doc"
    assert [(row["scope"], row["path"]) for row in document["items"]] == [
        ("project", "guides/setup.md"),
        ("organization", "glossary.md"),
    ]


def test_ls_narrows_by_scope_and_audience(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = make_tree(
        [
            make_doc("a.md", audience="agent"),
            make_doc("b.md", audience="human"),
            make_doc("c.md", audience="both"),
        ],
        [make_doc("d.md", scope="organization", audience="agent")],
    )
    _respond(tripl_api, TREE_URL, tree)
    argv = ["docs", "ls", "--project", "prod", "--scope", "project", "--audience", "agent"]
    assert main([*argv, "--json"]) == 0
    paths = [row["path"] for row in json.loads(capsys.readouterr().out)["items"]]
    # `both` is written for either reader, so it matches `agent` too.
    assert paths == ["a.md", "c.md"]


def test_ls_human_table_names_scope_path_and_title(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _respond(tripl_api, TREE_URL, make_tree([make_doc("guides/setup.md")], []))
    assert main(["docs", "ls", "--project", "prod"]) == 0
    out = capsys.readouterr().out
    assert "project  guides/setup.md  both  r2  setup" in out
    assert "1 doc." in out


# --- cat --------------------------------------------------------------------


def test_cat_prints_the_raw_note_and_nothing_else(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    content = "---\ntitle: Setup\n---\nSee [[event:gone]].\n"
    doc = {
        **make_doc("guides/setup.md"),
        "content": content,
        "links": [
            {"kind": "event", "target": "gone", "raw": "[[event:gone]]", "status": "broken"},
        ],
    }
    _respond(tripl_api, FILE_URL, doc)
    assert main(["docs", "cat", "guides/setup.md", "--project", "prod"]) == 0
    captured = capsys.readouterr()
    assert captured.out == content
    assert "warning: broken link [[event:gone]]" in captured.err
    request = _calls(tripl_api, "GET")[-1]
    assert request.url.params["scope"] == "project"
    assert request.url.params["path"] == "guides/setup.md"


def test_cat_json_carries_the_response_as_the_one_item(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _respond(tripl_api, FILE_URL, {**make_doc("x.md", scope="organization"), "content": "x"})
    argv = ["docs", "cat", "x.md", "--project", "prod", "--scope", "organization", "--json"]
    assert main(argv) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["kind"] == "doc_file"
    assert document["items"][0]["content"] == "x"
    assert _calls(tripl_api, "GET")[-1].url.params["scope"] == "organization"


def test_cat_lang_asks_for_the_translation_and_notes_a_fallback(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = {
        **make_doc("a.md"),
        "content": "# A\n",
        "lang": None,
        "requested_lang": "de",
        "translation_fallback": "missing",
    }
    _respond(tripl_api, FILE_URL, doc)
    assert main(["docs", "cat", "a.md", "--project", "prod", "--lang", "de"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "# A\n"
    assert "printed the original; the de translation is missing" in captured.err
    assert _calls(tripl_api, "GET")[-1].url.params["lang"] == "de"


def test_cat_json_notes_an_outdated_translation_on_stderr(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = {**make_doc("a.md"), "content": "# A\n", "lang": "de", "translation_outdated": True}
    _respond(tripl_api, FILE_URL, doc)
    assert main(["docs", "cat", "a.md", "--project", "prod", "--lang", "de", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["items"][0]["lang"] == "de"
    assert "the de translation is behind the original" in captured.err


def test_cat_without_lang_leaves_the_choice_to_the_server(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _respond(tripl_api, FILE_URL, {**make_doc("a.md"), "content": "x"})
    assert main(["docs", "cat", "a.md", "--project", "prod"]) == 0
    assert "lang" not in _calls(tripl_api, "GET")[-1].url.params
    assert capsys.readouterr().err == ""


# --- pull -------------------------------------------------------------------


def _bundle(*files: tuple[str, str]) -> dict[str, Any]:
    return {
        "format": "tripl-docs/v1",
        "scope": "project",
        "project_slug": "prod",
        "organization_slug": "default",
        "exported_at": "2026-09-27T10:00:00Z",
        "files": [{"path": path, "content": content, "sha256": None} for path, content in files],
    }


def test_pull_writes_every_note_under_the_folder(
    tripl_api: FakeInstance,
    configured_env: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _respond(tripl_api, EXPORT_URL, _bundle(("SKILL.md", "# S\r\n"), ("references/a.md", "a")))
    target = tmp_path / "out"
    assert main(["docs", "pull", str(target), "--project", "prod", "--json"]) == 0
    # Bytes, not text: the line endings the note was stored with survive.
    assert (target / "SKILL.md").read_bytes() == b"# S\r\n"
    assert (target / "references" / "a.md").read_text(encoding="utf-8") == "a"
    document = json.loads(capsys.readouterr().out)
    assert document["kind"] == "pulled_file"
    assert {row["action"] for row in document["items"]} == {"written"}
    assert _calls(tripl_api, "GET")[-1].url.params["format"] == "json"


def test_pull_refuses_a_non_empty_folder_without_force_and_sends_nothing(
    tripl_api: FakeInstance, configured_env: None, tmp_path: Path
) -> None:
    (tmp_path / "keep.txt").write_text("mine", encoding="utf-8")
    route = tripl_api.handler(EXPORT_URL, lambda request: httpx.Response(200, json=_bundle()))
    assert main(["docs", "pull", str(tmp_path), "--project", "prod"]) == 2
    assert route.call_count == 0


def test_pull_force_overwrites_the_exported_files_and_keeps_the_rest(
    tripl_api: FakeInstance, configured_env: None, tmp_path: Path
) -> None:
    (tmp_path / "keep.txt").write_text("mine", encoding="utf-8")
    (tmp_path / "a.md").write_text("old", encoding="utf-8")
    _respond(tripl_api, EXPORT_URL, _bundle(("a.md", "new")))
    assert main(["docs", "pull", str(tmp_path), "--project", "prod", "--force"]) == 0
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "new"
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "mine"


@pytest.mark.parametrize("unsafe", ["../escape.md", "/etc/passwd.md", "a/../../b.md", "a\\b.md"])
def test_pull_refuses_an_unsafe_path_and_writes_nothing_at_all(
    tripl_api: FakeInstance, configured_env: None, tmp_path: Path, unsafe: str
) -> None:
    _respond(tripl_api, EXPORT_URL, _bundle(("first.md", "ok"), (unsafe, "bad")))
    target = tmp_path / "out"
    assert main(["docs", "pull", str(target), "--project", "prod"]) == 1
    assert not (target / "first.md").exists()
    assert not (tmp_path / "escape.md").exists()


# --- push -------------------------------------------------------------------


def test_push_with_yes_sends_only_the_markdown_in_one_import(
    tripl_api: FakeInstance,
    configured_env: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _skill_folder(tmp_path / "checkout")
    _respond(tripl_api, IMPORT_URL, make_result(created=["SKILL.md"]), method="POST")
    assert main(["docs", "push", str(folder), "--project", "prod", "--yes"]) == 0
    posts = _calls(tripl_api, "POST")
    assert len(posts) == 1
    assert posts[0].url.params["mode"] == "merge"
    assert posts[0].url.params["dry_run"] == "false"
    assert posts[0].url.params["scope"] == "project"
    body = _body(posts[0])
    assert body["format"] == "tripl-docs/v1"
    assert [item["path"] for item in body["files"]] == ["SKILL.md", "references/events.md"]
    out = capsys.readouterr().out
    assert "scripts/run.sh (not a Markdown file)" in out
    assert "link.md (symbolic link)" in out
    assert ".git/ (hidden directory)" in out


def test_push_keep_root_prefixes_the_folder_name(
    tripl_api: FakeInstance, configured_env: None, tmp_path: Path
) -> None:
    folder = _skill_folder(tmp_path / "checkout")
    _respond(tripl_api, IMPORT_URL, make_result(), method="POST")
    assert main(["docs", "push", str(folder), "--project", "prod", "--yes", "--keep-root"]) == 0
    paths = [item["path"] for item in _body(_calls(tripl_api, "POST")[0])["files"]]
    assert paths == ["checkout/SKILL.md", "checkout/references/events.md"]


def test_push_on_a_non_tty_without_yes_refuses_and_sends_nothing(
    tripl_api: FakeInstance,
    configured_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _skill_folder(tmp_path / "checkout")
    monkeypatch.setattr("sys.stdin", _Stdin("", isatty=False))
    assert main(["docs", "push", str(folder), "--project", "prod", "--mirror"]) == 2
    assert _calls(tripl_api, "POST") == []
    err = capsys.readouterr().err
    assert "--yes" in err
    assert "DELETE every note" in err


def test_push_on_a_tty_previews_then_imports_after_yes(
    tripl_api: FakeInstance,
    configured_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _skill_folder(tmp_path / "checkout")
    _respond(tripl_api, IMPORT_URL, make_result(created=["SKILL.md"]), method="POST")
    monkeypatch.setattr("sys.stdin", _Stdin("y\n", isatty=True))
    assert main(["docs", "push", str(folder), "--project", "prod"]) == 0
    posts = _calls(tripl_api, "POST")
    assert [post.url.params["dry_run"] for post in posts] == ["true", "false"]
    assert "1 created" in capsys.readouterr().err


def test_push_declined_after_the_preview_exits_one_with_only_the_preview_sent(
    tripl_api: FakeInstance,
    configured_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = _skill_folder(tmp_path / "checkout")
    _respond(tripl_api, IMPORT_URL, make_result(updated=["SKILL.md"]), method="POST")
    monkeypatch.setattr("sys.stdin", _Stdin("n\n", isatty=True))
    assert main(["docs", "push", str(folder), "--project", "prod"]) == 1
    assert [post.url.params["dry_run"] for post in _calls(tripl_api, "POST")] == ["true"]


def test_push_dry_run_sends_nothing_and_prints_sizes_not_content(
    tripl_api: FakeInstance,
    configured_env: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = _skill_folder(tmp_path / "checkout")
    assert main(["docs", "push", str(folder), "--project", "prod", "--dry-run", "--json"]) == 0
    assert _calls(tripl_api, "POST") == []
    document = json.loads(capsys.readouterr().out)
    assert document["command"] == "docs push"
    assert document["dry_run"] is True
    assert document["result"] is None
    assert document["action"] == "merge"
    files = document["request"]["body"]["files"]
    assert files[0] == {"path": "SKILL.md", "size_bytes": 34}
    assert "content" not in files[0]


def test_push_refuses_an_empty_folder_under_mirror(
    tripl_api: FakeInstance, configured_env: None, tmp_path: Path
) -> None:
    (tmp_path / "notes.txt").write_text("not markdown", encoding="utf-8")
    argv = ["docs", "push", str(tmp_path), "--project", "prod", "--mirror", "--yes"]
    assert main(argv) == 2
    assert _calls(tripl_api, "POST") == []


def test_push_refuses_a_file_that_is_not_utf8(
    tripl_api: FakeInstance,
    configured_env: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "ok.md").write_text("fine", encoding="utf-8")
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe latin")
    assert main(["docs", "push", str(tmp_path), "--project", "prod", "--yes"]) == 2
    assert _calls(tripl_api, "POST") == []
    assert "bad.md: not UTF-8 text" in capsys.readouterr().err


def test_push_drops_a_byte_order_mark_so_the_frontmatter_still_parses(
    tripl_api: FakeInstance, configured_env: None, tmp_path: Path
) -> None:
    (tmp_path / "a.md").write_bytes("﻿---\ntitle: A\n---\n".encode())
    _respond(tripl_api, IMPORT_URL, make_result(), method="POST")
    assert main(["docs", "push", str(tmp_path), "--project", "prod", "--yes"]) == 0
    assert _body(_calls(tripl_api, "POST")[0])["files"][0]["content"].startswith("---\n")


def test_push_a_refused_import_exits_one(
    tripl_api: FakeInstance,
    configured_env: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "a.md").write_text("---\naudience: robots\n---\n", encoding="utf-8")
    detail = {"detail": {"errors": [{"path": "a.md", "detail": "audience must be one of"}]}}
    tripl_api.handler(IMPORT_URL, lambda request: httpx.Response(422, json=detail), method="POST")
    assert main(["docs", "push", str(tmp_path), "--project", "prod", "--yes"]) == 1
    assert "audience must be one of" in capsys.readouterr().err
