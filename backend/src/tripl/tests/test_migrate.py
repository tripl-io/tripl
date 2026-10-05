"""``python -m tripl.migrate``: the core's migrations first, then each extension's."""

from __future__ import annotations

from pathlib import Path

import pytest

from tripl import migrate


class _Point:
    def __init__(self, name: str, target: object) -> None:
        self.name = name
        self.value = f"fake:{name}"
        self._target = target

    def load(self) -> object:
        return self._target


def test_core_runs_first_then_extensions_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(migrate, "upgrade_core", lambda: ran.append("core"))
    monkeypatch.setattr(
        migrate,
        "entry_points",
        lambda group: [
            _Point("zeta", lambda: ran.append("zeta")),
            _Point("alpha", lambda: ran.append("alpha")),
        ],
    )

    migrate.main()

    assert ran == ["core", "alpha", "zeta"]


def test_without_extensions_it_is_alembic_upgrade_head(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        migrate.command, "upgrade", lambda config, rev: calls.append((config.config_file_name, rev))
    )
    monkeypatch.setattr(migrate, "entry_points", lambda group: [])

    migrate.main()

    assert calls == [(str(Path("alembic.ini")), "head")]


def test_a_non_callable_entry_point_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate, "entry_points", lambda group: [_Point("bad", object())])

    with pytest.raises(TypeError, match="is not callable"):
        migrate.extension_upgrades()
