"""Worker-side cache invalidation names the one organization's project list.

``cache.sync_delete`` drops exact keys, so the demo tick and a metrics
collection invalidate ``key_projects_list`` of the project's own organization
instead of SCANning every organization's list.
"""

from __future__ import annotations

import uuid

import pytest

from tripl import cache, realtime
from tripl.worker.tasks import demo_runtime

ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
OTHER_PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000c2")


class _FakeRedis:
    def __init__(self, keys: set[str]) -> None:
        self.keys = set(keys)
        self.scans: list[str] = []

    def delete(self, *keys: str) -> int:
        self.keys -= set(keys)
        return len(keys)

    def scan_iter(self, match: str, count: int) -> list[str]:
        self.scans.append(match)
        prefix = match.removesuffix("*")
        return [key for key in sorted(self.keys) if key.startswith(prefix)]


class _BrokenRedis:
    def delete(self, *keys: str) -> int:
        raise ConnectionError("redis is down")


def _keys() -> set[str]:
    return {
        cache.key_projects_list(ORG_A),
        cache.key_projects_list(ORG_B),
        cache.key_signals_all(PROJECT),
        cache.key_signals_all(OTHER_PROJECT),
    }


def test_sync_delete_drops_only_the_named_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis(_keys())
    monkeypatch.setattr(cache, "_get_sync_client", lambda: fake)
    cache.sync_delete(cache.key_projects_list(ORG_A))
    assert fake.keys == _keys() - {cache.key_projects_list(ORG_A)}
    assert fake.scans == []


def test_sync_delete_is_a_no_op_without_keys_or_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_client_wanted() -> None:
        raise AssertionError("no key, no round trip")

    monkeypatch.setattr(cache, "_get_sync_client", _no_client_wanted)
    cache.sync_delete()
    monkeypatch.setattr(cache, "_get_sync_client", lambda: None)
    cache.sync_delete(cache.key_projects_list(ORG_A))


def test_sync_delete_swallows_a_redis_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "_get_sync_client", lambda: _BrokenRedis())
    cache.sync_delete(cache.key_projects_list(ORG_A))


def test_the_demo_tick_invalidates_its_own_organization_and_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeRedis(_keys())
    monkeypatch.setattr(cache, "_get_sync_client", lambda: fake)
    published: list[tuple[uuid.UUID, str, str]] = []
    monkeypatch.setattr(
        realtime,
        "publish_project_event",
        lambda project_id, slug, event, _payload: published.append((project_id, slug, event)),
    )

    demo_runtime._emit_status(PROJECT, ORG_A, "demo")

    assert fake.keys == {cache.key_projects_list(ORG_B), cache.key_signals_all(OTHER_PROJECT)}
    # The project list is one exact key now; only the project's signals SCAN.
    assert fake.scans == [f"{cache.prefix_signals(PROJECT)}*"]
    assert [event for _project, _slug, event in published] == [
        realtime.EVENT_METRIC_COLLECTION_UPDATED,
        realtime.EVENT_SIGNALS_UPDATED,
    ]
