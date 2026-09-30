"""``tripl-admin``: the operator's shell tool for the platform-admin flag (F20 PR14).

Driven through :func:`tripl.admin_cli.run` against a sync SQLite database built
from the models (the suite's own database is async and in-memory, which a sync
engine cannot share), and once through :func:`main` with the session factory
patched, as the console script calls it.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl import admin_cli
from tripl.models import Base
from tripl.models.audit_log import AuditLog
from tripl.models.user import User
from tripl.tests._sqlite import enable_sqlite_foreign_keys


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'admin_cli.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    make = sessionmaker(engine, expire_on_commit=False)
    with make() as session:
        session.add_all(
            [
                User(email="ops@example.com", name="Ops", password_hash="x"),
                User(email="dev@example.com", name="Dev", password_hash="x"),
            ]
        )
        session.commit()
    try:
        yield make
    finally:
        engine.dispose()


def _run(factory: sessionmaker[Session], *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = admin_cli.run(list(argv), session_factory=factory, out=out)
    return code, out.getvalue()


def _admins(factory: sessionmaker[Session]) -> list[str]:
    with factory() as session:
        return list(
            session.scalars(
                select(User.email).where(User.is_platform_admin.is_(True)).order_by(User.email)
            ).all()
        )


def _audit(factory: sessionmaker[Session]) -> list[tuple[str, str]]:
    with factory() as session:
        rows = session.execute(
            select(AuditLog.action, AuditLog.target_name, AuditLog.organization_id)
        ).all()
    assert all(org_id is None for _action, _name, org_id in rows)
    # Sorted: rows written within one second tie on SQLite's created_at.
    return sorted((action, name) for action, name, _org in rows)


def test_grant_list_and_revoke(factory: sessionmaker[Session]) -> None:
    code, out = _run(factory, "list-platform-admins")
    assert (code, out.strip()) == (0, "No platform admins")

    code, out = _run(factory, "grant-platform-admin", "  OPS@Example.com ")
    assert code == 0
    assert "Granted platform admin to ops@example.com" in out
    assert _admins(factory) == ["ops@example.com"]

    code, out = _run(factory, "grant-platform-admin", "ops@example.com")
    assert code == 0
    assert "already a platform admin" in out

    assert _run(factory, "grant-platform-admin", "dev@example.com")[0] == 0
    code, out = _run(factory, "list-platform-admins")
    assert (code, out.split()) == (0, ["dev@example.com", "ops@example.com"])

    code, out = _run(factory, "revoke-platform-admin", "dev@example.com")
    assert code == 0
    assert "Revoked platform admin from dev@example.com" in out
    assert _admins(factory) == ["ops@example.com"]

    # Audited at platform scope, no user, once per actual change.
    assert _audit(factory) == [
        ("platform.admin_grant", "dev@example.com"),
        ("platform.admin_grant", "ops@example.com"),
        ("platform.admin_revoke", "dev@example.com"),
    ]


def _verified(factory: sessionmaker[Session], email: str) -> bool:
    with factory() as session:
        stamp = session.scalar(select(User.email_verified_at).where(User.email == email))
    return stamp is not None


def test_a_grant_marks_the_address_verified(factory: sessionmaker[Session]) -> None:
    assert not _verified(factory, "ops@example.com")
    assert _run(factory, "grant-platform-admin", "ops@example.com")[0] == 0
    assert _verified(factory, "ops@example.com")
    with factory() as session:
        payload = session.scalar(
            select(AuditLog.payload).where(AuditLog.action == "platform.admin_grant")
        )
    assert payload == {"email": "ops@example.com", "via": "tripl-admin", "marked_verified": True}

    # Already verified: the grant row does not claim it.
    with factory() as session:
        dev = session.scalar(select(User).where(User.email == "dev@example.com"))
        assert dev is not None
        dev.email_verified_at = datetime.now(UTC)
        session.commit()
    assert _run(factory, "grant-platform-admin", "dev@example.com")[0] == 0
    with factory() as session:
        payload = session.scalar(
            select(AuditLog.payload).where(
                AuditLog.action == "platform.admin_grant",
                AuditLog.target_name == "dev@example.com",
            )
        )
    assert payload == {"email": "dev@example.com", "via": "tripl-admin"}


def test_regranting_an_unverified_admin_only_verifies_it(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        ops = session.scalar(select(User).where(User.email == "ops@example.com"))
        assert ops is not None
        ops.is_platform_admin = True
        session.commit()
    code, out = _run(factory, "grant-platform-admin", "ops@example.com")
    assert code == 0
    assert "already a platform admin" in out
    assert _verified(factory, "ops@example.com")
    assert _audit(factory) == []


def test_the_last_platform_admin_cannot_be_revoked(
    factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(factory, "grant-platform-admin", "ops@example.com")[0] == 0
    code, _out = _run(factory, "revoke-platform-admin", "ops@example.com")
    assert code == admin_cli.EXIT_REFUSED
    assert "last platform admin" in capsys.readouterr().err
    assert _admins(factory) == ["ops@example.com"]


def test_revoking_a_non_admin_is_a_no_op(factory: sessionmaker[Session]) -> None:
    code, out = _run(factory, "revoke-platform-admin", "dev@example.com")
    assert code == 0
    assert "is not a platform admin" in out
    assert _audit(factory) == []


@pytest.mark.parametrize("command", ["grant-platform-admin", "revoke-platform-admin"])
def test_an_unknown_email_exits_non_zero(
    factory: sessionmaker[Session], command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _out = _run(factory, command, "nobody@example.com")
    assert code == admin_cli.EXIT_UNKNOWN_USER
    assert "No account with email nobody@example.com" in capsys.readouterr().err


def test_a_usage_error_exits_non_zero(factory: sessionmaker[Session]) -> None:
    assert _run(factory)[0] != 0
    assert _run(factory, "grant-platform-admin")[0] != 0
    assert _run(factory, "make-me-admin", "ops@example.com")[0] != 0


def test_main_is_the_console_script_entry(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(admin_cli, "_default_session", factory)
    assert admin_cli.main(["grant-platform-admin", "dev@example.com"]) == 0
    assert "Granted platform admin to dev@example.com" in capsys.readouterr().out
    assert _admins(factory) == ["dev@example.com"]
