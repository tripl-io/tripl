"""Stored translations of docs catalog notes: asking, the worker run, serving, editing."""

from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from tripl.main import app
from tripl.models.doc_translation import DocTranslation
from tripl.services import docs_translations, llm_service
from tripl.services._docs_translation_rows import PENDING_STALE_AFTER
from tripl.tests._docs_helpers import create_project, get_doc, put_doc, register
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import docs_translate

BASE = "/api/v1/projects/{slug}/docs"
NOTE = "---\ntitle: Funnel\n---\n# Funnel\n\nRead [[event:checkout]] first.\n"
AI_ON = SimpleNamespace(
    ai_enabled=True,
    ai_api_key="key",
    ai_model="test",
    ai_max_output_tokens=100,
    ai_base_url="https://example.com",
    ai_timeout_seconds=1,
    host_guard=False,
)


def _url(slug: str, suffix: str = "") -> str:
    return BASE.format(slug=slug) + suffix


def fake_model(system: str, user: str, **_kwargs: Any) -> str:
    """Upper-cases prose and keeps placeholders; names languages; answers JSON as JSON."""
    if "names a human language" in system:
        return json.dumps({"code": {"немецкий": "de"}.get(user)})
    if user.startswith("{"):
        return json.dumps({key: value.upper() for key, value in json.loads(user).items()})
    return re.sub(r"(⟦T\d+⟧)|[a-z]+", lambda m: m.group(1) or m.group(0).upper(), user)


@pytest.fixture
def ai(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """AI on, the fake model, and the queue recorded instead of sent."""
    queued: list[str] = []

    async def config(*_args: Any, **_kwargs: Any) -> Any:
        return AI_ON

    async def enqueue(translation_id: uuid.UUID) -> None:
        queued.append(str(translation_id))

    monkeypatch.setattr(docs_translations, "_ai_config", config)
    monkeypatch.setattr(docs_translations, "_enqueue", enqueue)
    monkeypatch.setattr(llm_service, "complete", fake_model)
    monkeypatch.setattr(
        docs_translate.app_settings_service, "get_ai_config_sync", lambda *a, **k: AI_ON
    )
    return queued


async def run_worker(monkeypatch: pytest.MonkeyPatch, translation_id: str) -> str:
    """The Celery task, run on a sync session over the test database."""
    async with TestSessionLocal() as session:

        def run(sync_session: Session) -> str:
            @contextmanager
            def shared() -> Iterator[Session]:
                yield sync_session

            monkeypatch.setattr(docs_translate, "_get_sync_session", shared)
            result: str = docs_translate.translate_doc.run(translation_id)
            return result

        return await session.run_sync(run)


async def translate(
    client: AsyncClient, slug: str, path: str, language: str, *, expect: int = 202, **extra: Any
) -> Any:
    resp = await client.post(
        _url(slug, "/translations"),
        json={"scope": "project", "path": path, "language": language, **extra},
    )
    assert resp.status_code == expect, resp.text
    return resp.json()


async def read(client: AsyncClient, slug: str, path: str, lang: str | None = None) -> Any:
    params = {"scope": "project", "path": path}
    if lang is not None:
        params["lang"] = lang
    resp = await client.get(_url(slug, "/file"), params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def write(client: AsyncClient, slug: str, lang: str, content: str, **extra: Any) -> Any:
    return await client.put(
        _url(slug, "/translations"),
        json={"scope": "project", "path": "a.md", "lang": lang, "content": content, **extra},
    )


async def test_translate_once_store_and_read(
    client: AsyncClient, ai: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_project(client, "tr")
    await put_doc(client, "tr", "a.md", NOTE)

    pending = await translate(client, "tr", "a.md", "немецкий")
    assert (pending["lang"], pending["status"], pending["revision"]) == ("de", "pending", 0)
    assert len(ai) == 1
    # Asking again while it runs is refused.
    await translate(client, "tr", "a.md", "de", expect=409)

    assert await run_worker(monkeypatch, ai[0]) == "ready"
    doc = await read(client, "tr", "a.md", "de")
    assert doc["lang"] == "de" and doc["translation_fallback"] is None
    assert doc["title"] == "FUNNEL"
    assert "[[event:checkout]]" in doc["content"] and "READ" in doc["body"]
    assert doc["revision"] == 1  # the original's
    assert [(t["lang"], t["status"], t["machine"]) for t in doc["translations"]] == [
        ("de", "ready", True)
    ]
    original = await read(client, "tr", "a.md", "original")
    assert original["lang"] is None and original["content"] == NOTE


async def test_agent_default_falls_back_when_missing_or_outdated(
    client: AsyncClient, ai: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_project(client, "agents")
    await put_doc(client, "agents", "a.md", NOTE)
    resp = await client.put(_url("agents", "/languages"), json={"agent_lang": "de"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"agent_lang": "de", "human_lang": None}

    # No translation yet: the original, saying why.
    doc = await read(client, "agents", "a.md")
    assert doc["lang"] is None
    assert (doc["requested_lang"], doc["translation_fallback"]) == ("de", "missing")

    await translate(client, "agents", "a.md", "de")
    assert (await read(client, "agents", "a.md"))["translation_fallback"] == "pending"
    await run_worker(monkeypatch, ai[0])
    assert (await read(client, "agents", "a.md"))["lang"] == "de"

    # The original moves on: the default read gets the original again, while a
    # read that names the language still gets the translation, marked.
    await put_doc(client, "agents", "a.md", NOTE + "More.\n", base_revision=1)
    default = await read(client, "agents", "a.md")
    assert (default["lang"], default["translation_fallback"]) == (None, "outdated")
    named = await read(client, "agents", "a.md", "de")
    assert named["lang"] == "de" and named["translation_outdated"] is True
    assert named["translations"][0]["outdated"] is True

    tree = await client.get(_url("agents"))
    assert tree.json()["language_defaults"] == {"agent_lang": "de", "human_lang": None}


async def test_edits_are_kept_and_retranslating_them_asks_first(
    client: AsyncClient, ai: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_project(client, "edit")
    await put_doc(client, "edit", "a.md", NOTE)
    await translate(client, "edit", "a.md", "de")
    await run_worker(monkeypatch, ai[0])

    edited = await write(
        client, "edit", "de", "# Trichter\n\nHand [[event:checkout]].\n", base_revision=1
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["revision"], edited.json()["machine"]) == (2, False)
    assert (await write(client, "edit", "de", "x", base_revision=1)).status_code == 409

    refused = await translate(client, "edit", "a.md", "de", expect=409)
    assert refused["detail"]["code"] == "translation_edited"
    await translate(client, "edit", "a.md", "de", overwrite=True)
    await run_worker(monkeypatch, ai[-1])

    history = await client.get(
        _url("edit", "/translations/revisions"),
        params={"scope": "project", "path": "a.md", "lang": "de"},
    )
    assert history.status_code == 200, history.text
    assert [(r["number"], r["action"]) for r in history.json()] == [
        (3, "translate"),
        (2, "edit"),
        (1, "translate"),
    ]
    hand = next(r for r in history.json() if r["action"] == "edit")
    detail = await client.get(
        _url("edit", f"/translations/revisions/{hand['id']}"),
        params={"scope": "project", "path": "a.md"},
    )
    assert detail.json()["content"].startswith("# Trichter")
    restored = await client.post(
        _url("edit", f"/translations/revisions/{hand['id']}/restore"),
        params={"scope": "project", "path": "a.md"},
    )
    assert restored.status_code == 200, restored.text
    assert (await read(client, "edit", "a.md", "de"))["body"].startswith("# Trichter")


async def test_mark_current_and_manual_translation(client: AsyncClient, ai: list[str]) -> None:
    await create_project(client, "manual")
    await put_doc(client, "manual", "a.md", NOTE)
    await put_doc(client, "manual", "a.md", NOTE + "v2\n", base_revision=1)

    # Written by hand, no AI run: it matches the current original.
    made = await write(client, "manual", "fr", "# Entonnoir\n")
    assert made.status_code == 200, made.text
    assert (made.json()["source_revision"], made.json()["outdated"]) == (2, False)

    await put_doc(client, "manual", "a.md", NOTE + "v3\n", base_revision=2)
    assert (await read(client, "manual", "a.md", "fr"))["translation_outdated"] is True
    current = await write(client, "manual", "fr", "# Entonnoir\n", mark_current=True)
    assert current.json()["outdated"] is False and current.json()["source_revision"] == 3

    gone = await client.delete(
        _url("manual", "/translations"), params={"scope": "project", "path": "a.md", "lang": "fr"}
    )
    assert gone.status_code == 204
    assert (await read(client, "manual", "a.md", "fr"))["translation_fallback"] == "missing"


async def test_worker_failure_and_a_superseded_run(
    client: AsyncClient, ai: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_project(client, "fail")
    await put_doc(client, "fail", "a.md", NOTE)
    await translate(client, "fail", "a.md", "de")
    monkeypatch.setattr(llm_service, "complete", lambda *a, **k: None)
    assert await run_worker(monkeypatch, ai[0]) == "failed"
    doc = await read(client, "fail", "a.md", "de")
    assert doc["lang"] is None and doc["translation_fallback"] == "failed"
    assert "request failed" in doc["translations"][0]["error"]

    # Asking again after a failure is allowed; a run whose row is no longer the
    # pending one that queued it writes nothing.
    monkeypatch.setattr(llm_service, "complete", fake_model)
    await translate(client, "fail", "a.md", "de")
    async with TestSessionLocal() as session:
        await session.execute(update(DocTranslation).values(status="ready"))
        await session.commit()
    assert await run_worker(monkeypatch, ai[-1]) == "skipped"


async def test_a_stuck_pending_run_can_be_asked_again(client: AsyncClient, ai: list[str]) -> None:
    await create_project(client, "stuck")
    await put_doc(client, "stuck", "a.md", NOTE)
    await translate(client, "stuck", "a.md", "de")
    async with TestSessionLocal() as session:
        await session.execute(
            update(DocTranslation).values(
                updated_at=datetime.now(UTC) - PENDING_STALE_AFTER - timedelta(minutes=1)
            )
        )
        await session.commit()
    doc = await read(client, "stuck", "a.md", "de")
    assert doc["translations"][0]["status"] == "failed"
    await translate(client, "stuck", "a.md", "de")


async def test_ai_off_and_bad_languages(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_project(client, "off")
    await put_doc(client, "off", "a.md", NOTE)
    off = SimpleNamespace(**{**AI_ON.__dict__, "ai_enabled": False})

    async def config(*_args: Any, **_kwargs: Any) -> Any:
        return off

    monkeypatch.setattr(docs_translations, "_ai_config", config)
    resp = await translate(client, "off", "a.md", "de", expect=409)
    assert "AI is not set up" in resp["detail"]
    # A name needs the model; a code does not.
    bad = await client.put(_url("off", "/languages"), json={"human_lang": "German"})
    assert bad.status_code == 422
    ok = await client.put(_url("off", "/languages"), json={"human_lang": "ru", "agent_lang": ""})
    assert ok.json() == {"agent_lang": None, "human_lang": "ru"}
    weird = await client.get(
        _url("off", "/file"), params={"scope": "project", "path": "a.md", "lang": "x y"}
    )
    assert weird.status_code == 422


async def test_viewers_read_translations_and_cannot_change_them(
    client: AsyncClient, ai: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_project(client, "roles")
    await put_doc(client, "roles", "a.md", NOTE)
    await translate(client, "roles", "a.md", "de")
    await run_worker(monkeypatch, ai[0])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as viewer:
        await register(viewer, "viewer@example.com", "Viewer")
        await add_member_by_slug("roles", "viewer@example.com", "viewer")
        assert (await read(viewer, "roles", "a.md", "de"))["lang"] == "de"
        await translate(viewer, "roles", "a.md", "fr", expect=403)
        assert (await write(viewer, "roles", "de", "x")).status_code == 403
        resp = await viewer.put(_url("roles", "/languages"), json={"agent_lang": "fr"})
        assert resp.status_code == 403


async def test_deleting_the_note_deletes_its_translations(
    client: AsyncClient, ai: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_project(client, "del")
    await put_doc(client, "del", "a.md", NOTE)
    await translate(client, "del", "a.md", "de")
    await run_worker(monkeypatch, ai[0])
    resp = await client.delete(_url("del", "/file"), params={"scope": "project", "path": "a.md"})
    assert resp.status_code == 204
    await put_doc(client, "del", "a.md", NOTE)
    assert (await get_doc(client, "del", "a.md"))["translations"] == []


async def test_export_and_import_carry_translations(
    client: AsyncClient, ai: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_project(client, "bundle")
    await put_doc(client, "bundle", "a.md", NOTE)
    await translate(client, "bundle", "a.md", "de")
    await run_worker(monkeypatch, ai[0])

    exported = await client.get(_url("bundle", "/export"), params={"scope": "project"})
    files = {item["path"]: item for item in exported.json()["files"]}
    assert set(files) == {"a.md", "a.de.md"}
    assert (files["a.de.md"]["translation_of"], files["a.de.md"]["lang"]) == ("a.md", "de")

    zipped = await client.get(
        _url("bundle", "/export"), params={"scope": "project", "format": "zip"}
    )
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        assert sorted(archive.namelist()) == ["a.de.md", "a.md"]

    # Into another project, as a zip: the name makes it a translation; a dotted
    # name with no note under its stem stays a note.
    await create_project(client, "copy")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.md", NOTE)
        archive.writestr("a.fr.md", "# Entonnoir\n")
        archive.writestr("release.en.md", "# Release notes\n")
    imported = await client.post(
        _url("copy", "/import/zip"),
        params={"scope": "project"},
        files={"file": ("docs.zip", buffer.getvalue(), "application/zip")},
    )
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert sorted(body["created"]) == ["a.md", "release.en.md"]
    assert body["translations"] == ["a.fr.md"]
    doc = await read(client, "copy", "a.md", "fr")
    assert doc["lang"] == "fr" and doc["translation_outdated"] is False

    # A mirror without the translation drops it.
    mirror = await client.post(
        _url("copy", "/import"),
        params={"scope": "project", "mode": "mirror"},
        json={
            "files": [
                {"path": "a.md", "content": NOTE},
                {"path": "release.en.md", "content": "# Release notes\n"},
            ]
        },
    )
    assert mirror.status_code == 200, mirror.text
    assert mirror.json()["translations_deleted"] == ["a.fr.md"]
    assert (await read(client, "copy", "a.md", "fr"))["translation_fallback"] == "missing"


async def test_import_refuses_a_translation_of_nothing(client: AsyncClient) -> None:
    await create_project(client, "orphan")
    resp = await client.post(
        _url("orphan", "/import"),
        params={"scope": "project", "dry_run": True},
        json={
            "files": [{"path": "x.de.md", "content": "# X", "translation_of": "x.md", "lang": "de"}]
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["errors"][0]["detail"] == "no note x.md to translate"
