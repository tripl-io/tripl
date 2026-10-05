"""The catalog of stored secrets, and re-encrypting every one (``services.stored_secrets``).

The catalog is what a key rotation walks, so a secret missing from it would stay
under a retired key and become unreadable: the guards here fail when a new
``*_encrypted`` column, or a new module that stores ``encrypt_value`` output,
appears without the catalog knowing.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from tripl import crypto
from tripl.config import settings
from tripl.extensions import Extension, override_extensions
from tripl.models import Base
from tripl.models.alert_destination import AlertDestination
from tripl.models.app_setting import SERVICE_SETTINGS_KEY, TRACKER_DEFAULTS_KEY, AppSetting
from tripl.models.data_source import DataSource
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.models.photo_storage_config import PhotoStorageConfig
from tripl.models.project import Project
from tripl.models.project_tracker_config import ProjectTrackerConfig
from tripl.schemas.data_source import SSLKEY_STORAGE_KEY
from tripl.services.stored_secrets import (
    CORE_SLOTS,
    ColumnSecret,
    JsonSecret,
    SecretSlot,
    rewrap_stored_secrets,
    stored_secret_slots,
)
from tripl.tests.conftest import TestSessionLocal

OLD_KEY = Fernet.generate_key()
NEW_KEY = Fernet.generate_key()
OLD = Fernet(OLD_KEY)
NEW = Fernet(NEW_KEY)
# Long ago, so a rewrap that bumped it would show.
STAMP = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)

SRC = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _old_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "encryption_key", OLD_KEY.decode())
    crypto.reset_cipher_cache()
    yield
    crypto.reset_cipher_cache()


def old_to_new(value: str) -> str:
    """A rotation in miniature: anything readable ends up under NEW."""
    try:
        NEW.decrypt(value.encode())
        return value
    except InvalidToken:
        pass
    return NEW.encrypt(OLD.decrypt(value.encode())).decode()


def _same_time(value: datetime) -> bool:
    """SQLite hands the stamp back naive."""
    return value.replace(tzinfo=UTC) == STAMP


def under_new(value: str) -> str:
    return NEW.decrypt(value.encode()).decode()


# -- the catalog cannot rot ---------------------------------------------------


def test_every_encrypted_column_is_in_the_catalog() -> None:
    """A new ``*_encrypted`` column on any model must be listed (or rotation skips it)."""
    listed = {(s.table, s.column) for s in stored_secret_slots() if isinstance(s, ColumnSecret)}
    found = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.name.endswith("_encrypted")
    }
    assert found - listed == set(), "add these to tripl.services.stored_secrets"


def test_every_slot_names_a_real_column() -> None:
    for slot in stored_secret_slots():
        table = Base.metadata.tables[slot.table]
        assert slot.column in table.c, slot.name
        if isinstance(slot, JsonSecret):
            assert slot.keys, slot.name
            if slot.where_column is not None:
                assert slot.where_column in table.c, slot.name
                assert slot.where_values, slot.name


# Every module that stores what ``encrypt_value`` returns, and the slot(s) that
# cover it. A module new to this list is a new stored secret: catalog it, then
# add it here (or, like instance_login, say why it is not stored).
_ENCRYPTING_MODULES: dict[str, str] = {
    "services/_alerting_destinations.py": "alert_destinations.*_encrypted",
    "services/datasource_service.py": "data_sources.password_encrypted, extra_params",
    "services/project_tracker_config_service.py": "project_tracker_configs.api_token_encrypted",
    "services/org_tracker_defaults_service.py": "app_settings.value[tracker_defaults]",
    "services/app_settings_service.py": "app_settings.value[settings]",
    "services/_app_settings_core.py": "app_settings.value[settings]",
    "services/photo_storage_service.py": "photo_storage_configs.value[credentials_json]",
    # The sign-in state cookie: short-lived, never stored.
    "services/instance_login.py": "not stored",
}


def test_every_module_storing_encrypted_values_is_accounted_for() -> None:
    calls = re.compile(r"\bencrypt_value\(")
    found = {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "tests" not in path.relative_to(SRC).parts
        and path.name != "crypto.py"
        and calls.search(path.read_text(encoding="utf-8"))
    }
    assert found == set(_ENCRYPTING_MODULES), "catalog the new stored secret, then list it here"


def test_core_slots_cover_the_json_secrets() -> None:
    names = {slot.name for slot in CORE_SLOTS}
    assert f"data_sources.extra_params[{SSLKEY_STORAGE_KEY}]" in names
    assert "app_settings.value[settings]" in names
    assert f"app_settings.value[{TRACKER_DEFAULTS_KEY}]" in names
    assert "photo_storage_configs.value[credentials_json]" in names


class _Slots(Extension):
    name = "slots"

    def __init__(self, *slots: SecretSlot) -> None:
        self._slots = slots

    def stored_secret_slots(self) -> tuple[SecretSlot, ...]:
        return self._slots


def test_extension_slots_follow_the_core_ones() -> None:
    extra = ColumnSecret("users", "name")
    with override_extensions([_Slots(extra)]):
        slots = stored_secret_slots()
    assert slots[: len(CORE_SLOTS)] == CORE_SLOTS
    assert slots[-1] == extra


def test_a_slot_listed_twice_is_refused() -> None:
    with (
        override_extensions([_Slots(ColumnSecret("data_sources", "password_encrypted"))]),
        pytest.raises(ValueError, match="listed twice"),
    ):
        stored_secret_slots()


def test_the_default_extension_has_no_slots() -> None:
    assert tuple(Extension().stored_secret_slots()) == ()


# -- rewrap -------------------------------------------------------------------


async def _seed() -> dict[str, uuid.UUID]:
    """One row per slot, each secret under OLD, plus empties that must stay empty."""
    project = Project(name="Secrets", slug="secrets")
    async with TestSessionLocal() as session:
        session.add(project)
        await session.flush()
        sources = [
            DataSource(
                name=f"src-{i}",
                db_type="postgres",
                host="db.example.com",
                port=5432,
                database_name="events",
                password_encrypted=crypto.encrypt_value(f"pw-{i}"),
                extra_params=(
                    {"sslmode": "require", SSLKEY_STORAGE_KEY: crypto.encrypt_value("K")}
                    if i == 0
                    else None
                ),
            )
            for i in range(5)
        ]
        no_password = DataSource(
            name="no-password",
            db_type="clickhouse",
            host="ch.example.com",
            port=8123,
            database_name="default",
            password_encrypted="",
        )
        destination = AlertDestination(
            project_id=project.id,
            type="slack",
            name="Slack",
            webhook_url_encrypted=crypto.encrypt_value("https://hooks.slack.com/services/x"),
            bot_token_encrypted=None,
        )
        tracker = ProjectTrackerConfig(
            project_id=project.id, api_token_encrypted=crypto.encrypt_value("jira-token")
        )
        service = AppSetting(
            key=SERVICE_SETTINGS_KEY,
            organization_id=None,
            value={"ai_api_key": crypto.encrypt_value("sk-1"), "ai_model": "gpt"},
        )
        defaults = AppSetting(
            key=TRACKER_DEFAULTS_KEY,
            organization_id=DEFAULT_ORG_ID,
            value={"jira_api_token": crypto.encrypt_value("org-jira"), "jira_base_url": "u"},
        )
        photos = PhotoStorageConfig(
            organization_id=DEFAULT_ORG_ID,
            config_hash="h" * 64,
            backend="gcs",
            value={"bucket": "b", "credentials_json": crypto.encrypt_value('{"type": "sa"}')},
        )
        session.add_all([*sources, no_password, destination, tracker, service, defaults, photos])
        await session.flush()
        for row in (*sources, destination, service):
            row.updated_at = STAMP
        await session.commit()
        return {
            "source0": sources[0].id,
            "no_password": no_password.id,
            "destination": destination.id,
            "tracker": tracker.id,
            "service": service.id,
            "defaults": defaults.id,
            "photos": photos.id,
        }


async def test_rewrap_moves_every_stored_secret_to_the_new_key() -> None:
    ids = await _seed()
    async with TestSessionLocal() as session:
        counts = await rewrap_stored_secrets(session, old_to_new, batch_size=2)

    assert counts["data_sources.password_encrypted"].rewritten == 5
    assert counts["alert_destinations.webhook_url_encrypted"].rewritten == 1
    assert counts["alert_destinations.bot_token_encrypted"].rewritten == 0
    assert counts["project_tracker_configs.api_token_encrypted"].rewritten == 1
    assert counts[f"data_sources.extra_params[{SSLKEY_STORAGE_KEY}]"].rewritten == 1
    assert counts["app_settings.value[settings]"].rewritten == 1
    assert counts[f"app_settings.value[{TRACKER_DEFAULTS_KEY}]"].rewritten == 1
    assert counts["photo_storage_configs.value[credentials_json]"].rewritten == 1
    assert all(c.unreadable == 0 for c in counts.values())

    async with TestSessionLocal() as session:
        source = await session.get(DataSource, ids["source0"])
        assert source is not None and source.extra_params is not None
        assert under_new(source.password_encrypted) == "pw-0"
        assert under_new(str(source.extra_params[SSLKEY_STORAGE_KEY])) == "K"
        assert source.extra_params["sslmode"] == "require"
        assert _same_time(source.updated_at), "a rewrap is not an edit"
        empty = await session.get(DataSource, ids["no_password"])
        assert empty is not None and empty.password_encrypted == ""
        destination = await session.get(AlertDestination, ids["destination"])
        assert destination is not None and destination.webhook_url_encrypted is not None
        assert under_new(destination.webhook_url_encrypted).startswith("https://hooks.slack")
        assert destination.bot_token_encrypted is None
        assert _same_time(destination.updated_at)
        tracker = await session.get(ProjectTrackerConfig, ids["tracker"])
        assert tracker is not None and under_new(tracker.api_token_encrypted) == "jira-token"
        service = await session.get(AppSetting, ids["service"])
        assert service is not None
        assert under_new(service.value["ai_api_key"]) == "sk-1"
        assert service.value["ai_model"] == "gpt"
        defaults = await session.get(AppSetting, ids["defaults"])
        assert defaults is not None
        assert under_new(defaults.value["jira_api_token"]) == "org-jira"
        assert defaults.value["jira_base_url"] == "u"
        photos = await session.get(PhotoStorageConfig, ids["photos"])
        assert photos is not None
        assert under_new(photos.value["credentials_json"]) == '{"type": "sa"}'
        assert photos.value["bucket"] == "b"

    # Everything is under the new key now: a second run changes nothing.
    async with TestSessionLocal() as session:
        again = await rewrap_stored_secrets(session, old_to_new, batch_size=2)
    assert sum(c.rewritten for c in again.values()) == 0
    assert again["data_sources.password_encrypted"].unchanged == 5


async def test_dry_run_counts_and_writes_nothing() -> None:
    ids = await _seed()
    async with TestSessionLocal() as session:
        counts = await rewrap_stored_secrets(session, old_to_new, batch_size=3, dry_run=True)
    assert counts["data_sources.password_encrypted"].rewritten == 5
    assert counts["app_settings.value[settings]"].rewritten == 1
    async with TestSessionLocal() as session:
        source = await session.get(DataSource, ids["source0"])
        assert source is not None
        assert OLD.decrypt(source.password_encrypted.encode()) == b"pw-0"


async def test_an_unreadable_value_is_counted_and_left_alone() -> None:
    ids = await _seed()
    stranger = Fernet(Fernet.generate_key()).encrypt(b"lost").decode()
    async with TestSessionLocal() as session:
        source = await session.get(DataSource, ids["source0"])
        assert source is not None
        source.password_encrypted = stranger
        await session.commit()

    async with TestSessionLocal() as session:
        counts = await rewrap_stored_secrets(session, old_to_new)
    assert counts["data_sources.password_encrypted"].unreadable == 1
    assert counts["data_sources.password_encrypted"].rewritten == 4

    async with TestSessionLocal() as session:
        source = await session.get(DataSource, ids["source0"])
        assert source is not None and source.password_encrypted == stranger


async def test_any_other_transform_error_stops_the_run() -> None:
    await _seed()

    def broken(_value: str) -> str:
        raise ConnectionError("key service down")

    async with TestSessionLocal() as session:
        with pytest.raises(ConnectionError):
            await rewrap_stored_secrets(session, broken)


async def test_rewrap_only_visits_the_slots_asked_for() -> None:
    ids = await _seed()
    seen: list[str] = []

    def record(value: str) -> str:
        seen.append(value)
        return old_to_new(value)

    only = (ColumnSecret("project_tracker_configs", "api_token_encrypted"),)
    async with TestSessionLocal() as session:
        counts = await rewrap_stored_secrets(session, record, slots=only)
    assert list(counts) == ["project_tracker_configs.api_token_encrypted"]
    assert len(seen) == 1
    async with TestSessionLocal() as session:
        password = (
            await session.execute(select(DataSource.password_encrypted).limit(1))
        ).scalar_one()
        assert OLD.decrypt(password.encode())
        tracker = await session.get(ProjectTrackerConfig, ids["tracker"])
        assert tracker is not None and under_new(tracker.api_token_encrypted) == "jira-token"


async def test_batch_size_must_be_positive() -> None:
    async with TestSessionLocal() as session:
        with pytest.raises(ValueError):
            await rewrap_stored_secrets(session, old_to_new, batch_size=0)
