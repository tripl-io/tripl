"""Docs catalog import/export: JSON bundles and zip folders (F22, GH #299)."""

import hashlib
import io
import stat
import zipfile

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from tripl.main import app
from tripl.services.docs_access import ORG_NOTES_ADMIN_REQUIRED
from tripl.services.docs_bundle import parse_zip_upload
from tripl.services.docs_paths import MAX_FILE_BYTES, MAX_ZIP_UPLOAD_BYTES
from tripl.tests._docs_helpers import create_project, get_doc, put_doc, register
from tripl.tests._members import add_member_by_slug

SKILL = (
    "---\n"
    "name: event-query-recipes\n"
    "description: How to query events in the warehouse\n"
    "allowed-tools: Read, Grep\n"
    "metadata:\n  version: 2\n"
    "---\n"
    "# Recipes\n"
)


def _zip(entries: dict[str, bytes | str], *, symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


def test_zip_keeps_markdown_strips_the_wrapper_and_reports_the_rest() -> None:
    data = _zip(
        {
            "my-skill/SKILL.md": SKILL,
            "my-skill/references/schema.md": "schema",
            "my-skill/scripts/run.py": "print()",
            "my-skill/assets/logo.png": b"\x89PNG",
            "__MACOSX/my-skill/._SKILL.md": b"\x00",
        },
        symlink="my-skill/references/link.md",
    )

    files, skipped, errors = parse_zip_upload(data)

    assert [(item.path, item.content) for item in files] == [
        ("SKILL.md", SKILL),
        ("references/schema.md", "schema"),
    ]
    assert {(item.path, item.reason) for item in skipped} == {
        ("my-skill/scripts/run.py", "not a Markdown (.md) file"),
        ("my-skill/assets/logo.png", "not a Markdown (.md) file"),
        ("__MACOSX/my-skill/._SKILL.md", "macOS metadata"),
        ("my-skill/references/link.md", "symbolic link"),
    }
    assert errors == []

    kept, _, _ = parse_zip_upload(data, keep_root=True)
    assert kept[0].path == "my-skill/SKILL.md"


def test_zip_guards() -> None:
    with pytest.raises(HTTPException) as too_big:
        parse_zip_upload(b"x" * (MAX_ZIP_UPLOAD_BYTES + 1))
    assert too_big.value.status_code == 413
    with pytest.raises(HTTPException) as not_zip:
        parse_zip_upload(b"not a zip")
    assert not_zip.value.status_code == 422

    _, _, errors = parse_zip_upload(
        _zip(
            {
                "big.md": "a" * (MAX_FILE_BYTES + 1),
                "latin1.md": "café".encode("latin-1"),
                "bomb.md": "a" * 200_000,
            }
        )
    )
    assert {(error.path, error.detail) for error in errors} == {
        ("big.md", "larger than 256 KiB"),
        ("latin1.md", "not valid UTF-8 text"),
        ("bomb.md", "compression ratio is too high"),
    }


async def test_json_round_trip_is_lossless(client: AsyncClient) -> None:
    await create_project(client, "roundtrip")
    await put_doc(client, "roundtrip", "SKILL.md", SKILL)
    await put_doc(client, "roundtrip", "references/a.md", "---\naudience: agent\n---\nA")

    exported = await client.get(
        "/api/v1/projects/roundtrip/docs/export", params={"scope": "project"}
    )
    assert exported.status_code == 200, exported.text
    bundle = exported.json()
    assert bundle["format"] == "tripl-docs/v1"
    assert bundle["project_slug"] == "roundtrip"
    assert bundle["organization_slug"] == "default"
    assert [item["path"] for item in bundle["files"]] == ["references/a.md", "SKILL.md"]

    await create_project(client, "copy")
    imported = await client.post(
        "/api/v1/projects/copy/docs/import",
        params={"scope": "project"},
        json={"format": "tripl-docs/v1", "files": bundle["files"]},
    )
    assert imported.status_code == 200, imported.text
    assert sorted(imported.json()["created"]) == ["SKILL.md", "references/a.md"]

    skill = await get_doc(client, "copy", "SKILL.md")
    assert skill["content"] == SKILL
    assert skill["title"] == "event-query-recipes"
    assert skill["extra_frontmatter"] == {
        "name": "event-query-recipes",
        "allowed-tools": "Read, Grep",
        "metadata": {"version": 2},
    }

    again = await client.post(
        "/api/v1/projects/copy/docs/import",
        params={"scope": "project"},
        json={"files": bundle["files"]},
    )
    assert sorted(again.json()["unchanged"]) == ["SKILL.md", "references/a.md"]


async def test_zip_export(client: AsyncClient) -> None:
    await create_project(client, "zipped")
    await put_doc(client, "zipped", "b.md", "B")
    await put_doc(client, "zipped", "a/c.md", "C")

    resp = await client.get(
        "/api/v1/projects/zipped/docs/export", params={"scope": "project", "format": "zip"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"] == 'attachment; filename="zipped-docs.zip"'
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        assert archive.namelist() == ["a/c.md", "b.md"]
        assert archive.read("b.md") == b"B"


async def test_merge_mirror_dry_run_and_errors(client: AsyncClient) -> None:
    await create_project(client, "mirrored")
    await put_doc(client, "mirrored", "keep.md", "same")
    await put_doc(client, "mirrored", "change.md", "old")
    await put_doc(client, "mirrored", "stale.md", "gone soon")
    files = [
        {"path": "keep.md", "content": "same"},
        {"path": "change.md", "content": "new"},
        {"path": "fresh.md", "content": "new file"},
        {"path": "script.sh", "content": "echo"},
    ]

    preview = await client.post(
        "/api/v1/projects/mirrored/docs/import",
        params={"scope": "project", "mode": "mirror", "dry_run": "true"},
        json={"files": files},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert (body["created"], body["updated"], body["unchanged"], body["deleted"]) == (
        ["fresh.md"],
        ["change.md"],
        ["keep.md"],
        ["stale.md"],
    )
    assert body["skipped"] == [{"path": "script.sh", "reason": "not a Markdown (.md) file"}]
    await get_doc(client, "mirrored", "stale.md")

    bad = await client.post(
        "/api/v1/projects/mirrored/docs/import",
        params={"scope": "project"},
        json={"files": [*files, {"path": "../x.md", "content": "x"}]},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["errors"][0]["path"] == "../x.md"
    await get_doc(client, "mirrored", "fresh.md", expect=404)

    applied = await client.post(
        "/api/v1/projects/mirrored/docs/import",
        params={"scope": "project", "mode": "mirror"},
        json={"files": files},
    )
    assert applied.status_code == 200, applied.text
    await get_doc(client, "mirrored", "stale.md", expect=404)
    changed = await get_doc(client, "mirrored", "change.md")
    assert changed["content"] == "new" and changed["revision"] == 2

    audit = await client.get(
        "/api/v1/audit", params={"project_slug": "mirrored", "action": "doc.import"}
    )
    assert audit.json()["total"] == 1
    entry = await client.get(f"/api/v1/audit/{audit.json()['items'][0]['id']}")
    assert entry.json()["payload"]["deleted"] == [
        {
            "path": "stale.md",
            "revision": 1,
            "content_sha256": hashlib.sha256(b"gone soon").hexdigest(),
        }
    ]


async def test_duplicate_paths_in_a_bundle_are_an_error(client: AsyncClient) -> None:
    await create_project(client, "dupes")
    resp = await client.post(
        "/api/v1/projects/dupes/docs/import",
        params={"scope": "project", "dry_run": "true"},
        json={"files": [{"path": "A.md", "content": "1"}, {"path": "a.md", "content": "2"}]},
    )
    assert resp.status_code == 200
    assert "case-insensitive" in resp.json()["errors"][0]["detail"]


async def test_zip_upload_endpoint(client: AsyncClient) -> None:
    await create_project(client, "uploaded")
    data = _zip({"pack/SKILL.md": SKILL, "pack/references/a.md": "A", "pack/x.py": "1"})

    resp = await client.post(
        "/api/v1/projects/uploaded/docs/import/zip",
        params={"scope": "project"},
        files={"file": ("pack.zip", data, "application/zip")},
    )
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["created"]) == ["SKILL.md", "references/a.md"]
    assert resp.json()["skipped"] == [{"path": "pack/x.py", "reason": "not a Markdown (.md) file"}]


async def test_importing_organization_notes_needs_an_org_owner_or_admin(
    client: AsyncClient,
) -> None:
    await create_project(client, "orgmirror")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as editor:
        member = await register(editor, "editor@example.com", "Editor")
        await add_member_by_slug("orgmirror", "editor@example.com", "editor")

        for mode in ("merge", "mirror"):
            refused = await editor.post(
                "/api/v1/projects/orgmirror/docs/import",
                params={"scope": "organization", "mode": mode},
                json={"files": [{"path": "a.md", "content": "x"}]},
            )
            assert refused.status_code == 403, refused.text
            assert refused.json()["detail"] == ORG_NOTES_ADMIN_REQUIRED

        promote = await client.patch(f"/api/v1/users/{member['id']}", json={"role": "admin"})
        assert promote.status_code == 200, promote.text
        merge = await editor.post(
            "/api/v1/projects/orgmirror/docs/import",
            params={"scope": "organization"},
            json={"files": [{"path": "a.md", "content": "x"}]},
        )
        assert merge.status_code == 200, merge.text
        admin_mirror = await editor.post(
            "/api/v1/projects/orgmirror/docs/import",
            params={"scope": "organization", "mode": "mirror"},
            json={"files": [{"path": "b.md", "content": "y"}]},
        )
        assert admin_mirror.status_code == 200, admin_mirror.text
        assert admin_mirror.json()["deleted"] == ["a.md"]

    owner_mirror = await client.post(
        "/api/v1/projects/orgmirror/docs/import",
        params={"scope": "organization", "mode": "mirror"},
        json={"files": []},
    )
    assert owner_mirror.status_code == 200
    assert owner_mirror.json()["deleted"] == ["b.md"]
