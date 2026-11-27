"""Uploaded ``tripl check`` runs: the ``check_upload`` key, the upload, the reads, retention.

The route half drives ``POST /projects/{slug}/check-runs``, the history
(``GET /check-runs``) and "used in code" (``GET /events/{id}/code-usage``)
through the app; the retention half runs ``maintenance.cleanup_check_runs``
on its own SQLite file, as the other retention tests do.

Every name here is synthetic.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.api.deps import CHECK_UPLOAD_SCOPE_ONLY
from tripl.models import Base
from tripl.models.check_run import CheckRun, CheckRunCall
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.services import project_permissions
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import maintenance

PASSWORD = "Password123!"
SHA_1 = "a" * 40
SHA_2 = "b" * 40


async def _issue_key(client: AsyncClient, scope: str, project_slug: str | None) -> str:
    payload: dict[str, object] = {"name": f"{scope}-key", "scope": scope}
    if project_slug is not None:
        payload["project_slug"] = project_slug
    resp = await client.post("/api/v1/me/api-keys", json=payload)
    assert resp.status_code == 201, resp.text
    token = resp.json()["token"]
    assert isinstance(token, str)
    return token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _project_with_event(client: AsyncClient, slug: str) -> tuple[str, str]:
    """A project with one event type and two events; returns the two event ids."""
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    assert event_type.status_code == 201, event_type.text
    ids = []
    for name in ("cart_opened", "cart_closed"):
        event = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={"event_type_id": event_type.json()["id"], "name": name},
        )
        assert event.status_code == 201, event.text
        ids.append(event.json()["id"])
    return ids[0], ids[1]


def _call(
    event_id: str | None, path: str, line: int, status: str = "ok", **extra: Any
) -> dict[str, Any]:
    return {
        "path": path,
        "line": line,
        "column": 5,
        "status": status,
        "event_id": event_id,
        "event_type": "track",
        "name": "cart_opened",
        "identity": "cart_opened",
        **extra,
    }


def _run(ref: str, calls: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "repository": "shop/web",
        "repository_url": "https://git.example.com/shop/web",
        "commit_sha": SHA_1,
        "ref": ref,
        "default_branch": "main",
        "pipeline_url": "https://git.example.com/shop/web/-/pipelines/7",
        "calls": calls,
        **extra,
    }


@pytest.mark.asyncio
async def test_a_check_upload_key_is_bound_to_a_project(client: AsyncClient) -> None:
    await client.post("/api/v1/projects", json={"name": "Bound", "slug": "cr-bound"})
    unbound = await client.post("/api/v1/me/api-keys", json={"name": "ci", "scope": "check_upload"})
    assert unbound.status_code == 422, unbound.text
    assert "project_slug" in unbound.json()["detail"]

    token = await _issue_key(client, "check_upload", "cr-bound")
    assert token.startswith("tk_c_")
    listed = await client.get("/api/v1/me/api-keys")
    assert listed.json()[0]["scope"] == "check_upload"


@pytest.mark.asyncio
async def test_check_upload_key_uploads_and_reads_and_writes_nothing_else(
    client: AsyncClient, anon_client: AsyncClient
) -> None:
    opened, _closed = await _project_with_event(client, "cr-scope")
    token = await _issue_key(client, "check_upload", "cr-scope")

    uploaded = await anon_client.post(
        "/api/v1/projects/cr-scope/check-runs",
        json=_run("main", [_call(opened, "src/cart.ts", 12)]),
        headers=_bearer(token),
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["is_current"] is True

    # It reads like a read key: the plan a check validates against.
    read = await anon_client.get("/api/v1/projects/cr-scope/event-types", headers=_bearer(token))
    assert read.status_code == 200, read.text
    validated = await anon_client.post(
        "/api/v1/projects/cr-scope/plan/validate",
        json={"items": [{"event_type": "track", "name": "cart_opened"}]},
        headers=_bearer(token),
    )
    assert validated.status_code == 200, validated.text

    # And any other write is refused.
    refused = await anon_client.post(
        "/api/v1/projects/cr-scope/event-types",
        json={"name": "other", "display_name": "Other"},
        headers=_bearer(token),
    )
    assert refused.status_code == 403
    assert refused.json()["detail"] == CHECK_UPLOAD_SCOPE_ONLY
    renamed = await anon_client.patch(
        f"/api/v1/projects/cr-scope/events/{opened}",
        json={"description": "changed"},
        headers=_bearer(token),
    )
    assert renamed.status_code == 403


@pytest.mark.asyncio
async def test_auth_me_never_answers_for_a_check_upload_key(
    client: AsyncClient, anon_client: AsyncClient
) -> None:
    # The key is always bound to one project, and /auth/me refuses a
    # project-bound key: ``tripl whoami`` tells it by its prefix instead.
    await client.post("/api/v1/projects", json={"name": "Me", "slug": "cr-me"})
    token = await _issue_key(client, "check_upload", "cr-me")
    me = await anon_client.get("/api/v1/auth/me", headers=_bearer(token))
    assert me.status_code == 403
    assert me.json()["detail"] == "API key is scoped to a single project"


@pytest.mark.asyncio
async def test_a_read_key_cannot_upload_and_a_write_key_can(
    client: AsyncClient, anon_client: AsyncClient
) -> None:
    opened, _closed = await _project_with_event(client, "cr-read")
    read_token = await _issue_key(client, "read", "cr-read")
    body = _run("main", [_call(opened, "src/cart.ts", 12)])
    refused = await anon_client.post(
        "/api/v1/projects/cr-read/check-runs", json=body, headers=_bearer(read_token)
    )
    assert refused.status_code == 403
    assert "read-only" in refused.json()["detail"]

    write_token = await _issue_key(client, "write", None)
    allowed = await anon_client.post(
        "/api/v1/projects/cr-read/check-runs", json=body, headers=_bearer(write_token)
    )
    assert allowed.status_code == 201, allowed.text


@pytest.mark.asyncio
async def test_a_viewer_uploads_with_a_check_upload_key_and_only_with_one(
    client: AsyncClient,
) -> None:
    opened, _closed = await _project_with_event(client, "cr-viewer")
    body = _run("main", [_call(opened, "src/cart.ts", 12)])
    await client.post("/api/v1/auth/logout")
    registered = await client.post(
        "/api/v1/auth/register",
        json={"email": "cr-viewer@example.com", "password": PASSWORD, "name": "Viewer"},
    )
    assert registered.status_code == 201, registered.text

    # Not a member yet: no project to upload to, and no key minted for it.
    hidden = await client.post("/api/v1/projects/cr-viewer/check-runs", json=body)
    assert hidden.status_code == 404
    minted = await client.post(
        "/api/v1/me/api-keys",
        json={"name": "ci", "scope": "check_upload", "project_slug": "cr-viewer"},
    )
    assert minted.status_code == 404, minted.text

    await add_member_by_slug("cr-viewer", "cr-viewer@example.com", "viewer")
    # The browser session of a viewer still may not record a run...
    refused = await client.post("/api/v1/projects/cr-viewer/check-runs", json=body)
    assert refused.status_code == 403
    # ...and the viewer's check_upload key may: it is the narrow permission.
    token = await _issue_key(client, "check_upload", "cr-viewer")
    uploaded = await client.post(
        "/api/v1/projects/cr-viewer/check-runs", json=body, headers=_bearer(token)
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["is_current"] is True
    # It writes nothing else, the viewer's role aside.
    other = await client.post(
        "/api/v1/projects/cr-viewer/event-types",
        json={"name": "other", "display_name": "Other"},
        headers=_bearer(token),
    )
    assert other.status_code == 403
    # A viewer reads the history and the usage.
    assert (await client.get("/api/v1/projects/cr-viewer/check-runs")).status_code == 200
    usage = await client.get(f"/api/v1/projects/cr-viewer/events/{opened}/code-usage")
    assert usage.status_code == 200, usage.text
    assert [len(repo["calls"]) for repo in usage.json()["repositories"]] == [1]

    # Taken out of the project, the key reaches nothing there.
    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == "cr-viewer"))
        user_id = await session.scalar(select(User.id).where(User.email == "cr-viewer@example.com"))
        await session.execute(
            delete(ProjectMember).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
            )
        )
        await session.commit()
    gone = await client.post(
        "/api/v1/projects/cr-viewer/check-runs", json=body, headers=_bearer(token)
    )
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_used_in_code_is_the_latest_default_branch_run_per_repository(
    client: AsyncClient,
) -> None:
    opened, closed = await _project_with_event(client, "cr-usage")
    base = "/api/v1/projects/cr-usage"
    first = await client.post(
        f"{base}/check-runs",
        json=_run(
            "main",
            [
                _call(
                    opened,
                    "src/cart.ts",
                    12,
                    status="warning",
                    findings=[
                        {
                            "code": "deprecated_event",
                            "severity": "warning",
                            "message": "deprecated",
                        }
                    ],
                ),
                _call(opened, "src/checkout.ts", 40),
                _call(closed, "src/cart.ts", 30),
                _call(None, "src/dynamic.ts", 3, status="dynamic"),
            ],
        ),
    )
    assert first.status_code == 201, first.text
    assert first.json()["calls"] == {"total": 4, "ok": 2, "warning": 1, "error": 0, "dynamic": 1}

    usage = (await client.get(f"{base}/events/{opened}/code-usage")).json()
    (repo,) = usage["repositories"]
    assert repo["repository"] == "shop/web"
    assert repo["run"]["commit_sha"] == SHA_1
    assert [(c["path"], c["line"]) for c in repo["calls"]] == [
        ("src/cart.ts", 12),
        ("src/checkout.ts", 40),
    ]
    assert repo["calls"][0]["findings"][0]["code"] == "deprecated_event"
    assert repo["calls"][0]["findings"][0]["suppressed"] is False

    # A merge request run goes to the history and leaves "used in code" alone.
    branch_run = await client.post(
        f"{base}/check-runs",
        json=_run("feature/x", [_call(closed, "src/new.ts", 1)], commit_sha=SHA_2),
    )
    assert branch_run.status_code == 201, branch_run.text
    assert branch_run.json()["is_default_branch"] is False
    assert branch_run.json()["is_current"] is False
    unchanged = (await client.get(f"{base}/events/{opened}/code-usage")).json()
    assert unchanged["repositories"][0]["run"]["id"] == first.json()["id"]

    # The next default-branch run replaces the snapshot: the removed call is gone.
    second = await client.post(
        f"{base}/check-runs",
        json=_run("main", [_call(closed, "src/cart.ts", 31)], commit_sha=SHA_2),
    )
    assert second.status_code == 201, second.text
    gone = (await client.get(f"{base}/events/{opened}/code-usage")).json()
    assert gone["repositories"][0]["run"]["id"] == second.json()["id"]
    assert gone["repositories"][0]["calls"] == []
    moved = (await client.get(f"{base}/events/{closed}/code-usage")).json()
    assert [(c["path"], c["line"]) for c in moved["repositories"][0]["calls"]] == [
        ("src/cart.ts", 31)
    ]

    # Another repository has its own snapshot.
    other = await client.post(
        f"{base}/check-runs",
        json=_run("main", [_call(opened, "App/Cart.swift", 9)], repository="shop/ios"),
    )
    assert other.status_code == 201, other.text
    both = (await client.get(f"{base}/events/{opened}/code-usage")).json()
    assert [r["repository"] for r in both["repositories"]] == ["shop/ios", "shop/web"]

    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == "cr-usage"))
        runs = (
            await session.scalars(select(CheckRun).where(CheckRun.project_id == project_id))
        ).all()
        stored = (await session.scalars(select(CheckRunCall))).all()
    # Only the current runs keep calls: shop/web's second and shop/ios's.
    assert sorted(run.is_current for run in runs) == [False, False, True, True]
    current = {run.id for run in runs if run.is_current}
    assert {call.run_id for call in stored} == current
    assert len(stored) == 2

    history = (await client.get(f"{base}/check-runs")).json()
    assert history["total"] == 4
    assert history["items"][0]["repository"] == "shop/ios"
    assert history["items"][-1]["id"] == first.json()["id"]
    assert history["items"][-1]["is_current"] is False
    assert history["items"][-1]["calls"]["total"] == 4
    only_web = (await client.get(f"{base}/check-runs?repository=shop/web")).json()
    assert only_web["total"] == 3
    default_only = (await client.get(f"{base}/check-runs?default_branch=true")).json()
    assert default_only["total"] == 3
    paged = (await client.get(f"{base}/check-runs?limit=1&offset=1")).json()
    assert paged["total"] == 4
    assert len(paged["items"]) == 1


@pytest.mark.asyncio
async def test_a_run_without_a_default_branch_is_history_only(client: AsyncClient) -> None:
    opened, _closed = await _project_with_event(client, "cr-nodefault")
    resp = await client.post(
        "/api/v1/projects/cr-nodefault/check-runs",
        json=_run("main", [_call(opened, "src/cart.ts", 1)], default_branch=None),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_current"] is False
    usage = await client.get(f"/api/v1/projects/cr-nodefault/events/{opened}/code-usage")
    assert usage.json()["repositories"] == []


@pytest.mark.asyncio
async def test_a_merge_request_run_never_replaces_the_default_branch_run(
    client: AsyncClient,
) -> None:
    # A merge request from a branch called like the default branch (main into
    # release, or a fork's main) checks a merged tree: history only.
    opened, _closed = await _project_with_event(client, "cr-mr")
    base = "/api/v1/projects/cr-mr"
    main_run = await client.post(
        f"{base}/check-runs", json=_run("main", [_call(opened, "src/cart.ts", 1)])
    )
    assert main_run.status_code == 201, main_run.text
    assert main_run.json()["merge_request"] is False

    request_run = await client.post(
        f"{base}/check-runs",
        json=_run("main", [_call(opened, "src/other.ts", 9)], merge_request=True, commit_sha=SHA_2),
    )
    assert request_run.status_code == 201, request_run.text
    body = request_run.json()
    assert (body["merge_request"], body["is_default_branch"], body["is_current"]) == (
        True,
        False,
        False,
    )

    usage = (await client.get(f"{base}/events/{opened}/code-usage")).json()
    assert [
        (repo["run"]["id"], [(call["path"], call["line"]) for call in repo["calls"]])
        for repo in usage["repositories"]
    ] == [(main_run.json()["id"], [("src/cart.ts", 1)])]


@pytest.mark.asyncio
async def test_an_event_id_of_another_project_is_not_trusted(client: AsyncClient) -> None:
    foreign, _ = await _project_with_event(client, "cr-foreign")
    opened, _ = await _project_with_event(client, "cr-home")
    resp = await client.post(
        "/api/v1/projects/cr-home/check-runs",
        json=_run("main", [_call(foreign, "src/a.ts", 1), _call(opened, "src/b.ts", 2)]),
    )
    assert resp.status_code == 201, resp.text
    async with TestSessionLocal() as session:
        stored = {
            call.path: call.event_id for call in (await session.scalars(select(CheckRunCall)))
        }
    assert stored == {"src/a.ts": None, "src/b.ts": uuid.UUID(opened)}
    # And another project's event has no usage here.
    missing = await client.get(f"/api/v1/projects/cr-home/events/{foreign}/code-usage")
    assert missing.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"pipeline_url": "javascript:alert(1)"},
        {"repository_url": "ftp://git.example.com/x"},
        {"commit_sha": "not-a-sha"},
        {"repository": ""},
    ],
)
async def test_an_upload_with_a_bad_field_is_refused(
    client: AsyncClient, change: dict[str, Any]
) -> None:
    await client.post("/api/v1/projects", json={"name": "Bad", "slug": "cr-bad"})
    resp = await client.post("/api/v1/projects/cr-bad/check-runs", json=_run("main", [], **change))
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_an_upload_is_capped_at_one_validate_batch(client: AsyncClient) -> None:
    from tripl.schemas.check_run import MAX_CHECK_RUN_CALLS

    await client.post("/api/v1/projects", json={"name": "Cap", "slug": "cr-cap"})
    call = _call(None, "a.ts", 1)
    over = await client.post(
        "/api/v1/projects/cr-cap/check-runs",
        json=_run("feature", [call] * (MAX_CHECK_RUN_CALLS + 1)),
    )
    assert over.status_code == 422


def test_the_upload_route_asks_for_its_own_permission() -> None:
    path = "/api/v1/projects/{slug}/check-runs"
    assert project_permissions.classify(path) == project_permissions.CHECK_RUNS_UPLOAD
    assert project_permissions.CHECK_RUNS_UPLOAD in project_permissions.PERMISSION_KEYS


@pytest.mark.asyncio
async def test_each_check_directory_of_a_repository_keeps_its_own_snapshot(
    client: AsyncClient,
) -> None:
    opened, _closed = await _project_with_event(client, "cr-dirs")
    base = "/api/v1/projects/cr-dirs"
    ios = await client.post(
        f"{base}/check-runs",
        json=_run("main", [_call(opened, "apps/ios/Cart.swift", 3)], check_root="apps/ios/"),
    )
    assert ios.status_code == 201, ios.text
    assert ios.json()["check_root"] == "apps/ios"
    web = await client.post(
        f"{base}/check-runs",
        json=_run("main", [_call(opened, "apps/web/cart.ts", 8)], check_root="./apps/web"),
    )
    assert web.status_code == 201, web.text
    root = await client.post(f"{base}/check-runs", json=_run("main", []))
    assert root.status_code == 201, root.text
    assert root.json()["check_root"] == "."

    # A second app in the repository did not replace the first one's snapshot.
    usage = (await client.get(f"{base}/events/{opened}/code-usage")).json()
    assert [
        (repo["repository"], repo["check_root"], [c["path"] for c in repo["calls"]])
        for repo in usage["repositories"]
    ] == [
        ("shop/web", ".", []),
        ("shop/web", "apps/ios", ["apps/ios/Cart.swift"]),
        ("shop/web", "apps/web", ["apps/web/cart.ts"]),
    ]

    # The next run of one directory replaces that directory's snapshot only.
    again = await client.post(
        f"{base}/check-runs",
        json=_run("main", [], check_root="apps/ios", commit_sha=SHA_2),
    )
    assert again.status_code == 201, again.text
    after = (await client.get(f"{base}/events/{opened}/code-usage")).json()
    assert {repo["check_root"]: len(repo["calls"]) for repo in after["repositories"]} == {
        ".": 0,
        "apps/ios": 0,
        "apps/web": 1,
    }
    only_ios = (await client.get(f"{base}/check-runs?check_root=apps/ios")).json()
    assert only_ios["total"] == 2
    assert [item["is_current"] for item in only_ios["items"]] == [True, False]
    # The filter names a directory the way an upload does: one directory, however written.
    for spelling in ("apps/ios/", "./apps/ios", "apps\\ios"):
        same = await client.get(f"{base}/check-runs", params={"check_root": spelling})
        assert same.json()["total"] == 2, spelling
    root_only = await client.get(f"{base}/check-runs", params={"check_root": "./"})
    assert root_only.json()["total"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["/abs/path", "..", "../other", "apps/../../x"])
async def test_a_history_filter_outside_the_repository_is_refused(
    client: AsyncClient, bad: str
) -> None:
    await client.post("/api/v1/projects", json={"name": "Dir", "slug": "cr-badfilter"})
    resp = await client.get("/api/v1/projects/cr-badfilter/check-runs", params={"check_root": bad})
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["/abs/path", "..", "../other", "apps/../../x"])
async def test_a_check_directory_outside_the_repository_is_refused(
    client: AsyncClient, bad: str
) -> None:
    await client.post("/api/v1/projects", json={"name": "Dir", "slug": "cr-baddir"})
    resp = await client.post(
        "/api/v1/projects/cr-baddir/check-runs", json=_run("main", [], check_root=bad)
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "late"),
    [
        # A re-run of an older pipeline that finishes last.
        (
            {"pipeline_created_at": "2026-11-20T10:00:00Z"},
            {"pipeline_created_at": "2026-11-19T10:00:00Z"},
        ),
        # No pipeline time on one side: the commit timestamps decide.
        (
            {"pipeline_created_at": "2026-11-20T10:00:00Z", "committed_at": "2026-11-20T09:00:00Z"},
            {"committed_at": "2026-11-19T12:00:00+02:00"},
        ),
    ],
)
async def test_an_older_default_branch_run_goes_to_the_history_only(
    client: AsyncClient, current: dict[str, str], late: dict[str, str]
) -> None:
    opened, closed = await _project_with_event(client, "cr-order")
    base = "/api/v1/projects/cr-order"
    newer = await client.post(
        f"{base}/check-runs", json=_run("main", [_call(opened, "src/new.ts", 1)], **current)
    )
    assert newer.status_code == 201, newer.text
    older = await client.post(
        f"{base}/check-runs",
        json=_run("main", [_call(closed, "src/old.ts", 2)], commit_sha=SHA_2, **late),
    )
    assert older.status_code == 201, older.text
    body = older.json()
    assert (body["is_default_branch"], body["is_current"]) == (True, False)

    usage = (await client.get(f"{base}/events/{opened}/code-usage")).json()
    assert usage["repositories"][0]["run"]["id"] == newer.json()["id"]
    assert [c["path"] for c in usage["repositories"][0]["calls"]] == ["src/new.ts"]
    assert (
        usage["repositories"][0]["run"]["pipeline_created_at"]
        == (newer.json()["pipeline_created_at"])
    )
    async with TestSessionLocal() as session:
        kept = (await session.scalars(select(CheckRunCall.path))).all()
    assert kept == ["src/new.ts"]


@pytest.mark.asyncio
async def test_a_newer_or_unordered_default_branch_run_replaces_the_current_one(
    client: AsyncClient,
) -> None:
    opened, _closed = await _project_with_event(client, "cr-newer")
    base = "/api/v1/projects/cr-newer"
    first = await client.post(
        f"{base}/check-runs",
        json=_run("main", [], pipeline_created_at="2026-11-20T10:00:00Z"),
    )
    assert first.json()["is_current"] is True
    newer = await client.post(
        f"{base}/check-runs",
        json=_run("main", [], pipeline_created_at="2026-11-21T10:00:00Z", commit_sha=SHA_2),
    )
    assert newer.json()["is_current"] is True
    # Nothing to compare with (no time sent): the later upload wins, as before.
    unordered = await client.post(f"{base}/check-runs", json=_run("main", []))
    assert unordered.json()["is_current"] is True
    history = (await client.get(f"{base}/check-runs?default_branch=true")).json()
    assert [item["is_current"] for item in history["items"]] == [True, False, False]


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'check_runs.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _stored_run(
    project_id: uuid.UUID, *, age_days: int, current: bool, repository: str = "shop/web"
) -> CheckRun:
    created = datetime.now(UTC) - timedelta(days=age_days)
    return CheckRun(
        id=uuid.uuid4(),
        project_id=project_id,
        repository=repository,
        commit_sha=SHA_1,
        ref="main",
        default_branch="main",
        is_default_branch=True,
        is_current=current,
        calls_total=0,
        calls_ok=0,
        calls_warning=0,
        calls_error=0,
        calls_dynamic=0,
        created_at=created,
        updated_at=created,
    )


def test_cleanup_keeps_current_runs_and_recent_summaries(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "_get_sync_session", session_factory)
    monkeypatch.setattr(maintenance.settings, "check_run_retention_days", 90)
    with session_factory() as session:
        project = Project(id=uuid.uuid4(), name="Retention", slug="retention", description="")
        session.add(project)
        session.commit()
        kept_current = _stored_run(project.id, age_days=400, current=True)
        kept_recent = _stored_run(project.id, age_days=10, current=False)
        dropped = _stored_run(project.id, age_days=91, current=False)
        session.add_all([kept_current, kept_recent, dropped])
        session.commit()

    result = maintenance.cleanup_check_runs()

    assert result["deleted"] == 1
    with session_factory() as session:
        left = set(session.scalars(select(CheckRun.id)).all())
    assert left == {kept_current.id, kept_recent.id}
