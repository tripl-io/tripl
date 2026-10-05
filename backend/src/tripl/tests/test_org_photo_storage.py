"""Per-organization photo storage (F20 PR11).

* resolution: the storage group is inherited whole (whatever the fallback
  policy), the size cap is clamped to the operator's and the content types are
  narrowed to the operator's allow-list;
* the save: an organization's own storage is a GCS bucket with a
  service-account JSON of its own (write-only, encrypted), never the local
  backend on a hosted instance; the server paths are not organization fields;
* uploads are keyed ``orgs/{org_id}/events/...`` and record the organization
  and the storage version they were written with, so a later change of the
  organization's storage never sends a read of an older photo elsewhere;
* drivers are cached per (organization, config fingerprint);
* the orphan sweep lists only the prefixes tripl writes;
* organization deletion deletes blobs through the organization's own storage;
* the CSP admits GCS images per request.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from tripl import crypto, database
from tripl.config import settings
from tripl.main import app
from tripl.middleware import security_headers
from tripl.middleware.org_context import OrgRef, bind_org, bound_org, reset_org
from tripl.models import Base
from tripl.models.app_setting import SERVICE_SETTINGS_KEY, AppSetting
from tripl.models.domain_enums import OrganizationStatus
from tripl.models.event_photo import EventPhoto
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.photo_storage_config import PhotoStorageConfig
from tripl.models.user import User
from tripl.services import (
    org_deletion_service,
    org_settings_service,
    org_storage_csp,
    photo_storage_service,
)
from tripl.services.app_settings_service import resolve_settings
from tripl.storage import photo_storage
from tripl.storage.photo_storage import (
    GOOGLE_TOKEN_URI,
    MissingStorageCredentials,
    StorageConfig,
    StoredObject,
    UnsafeServiceAccount,
    driver_for_config,
    reset_photo_storage,
)
from tripl.tests._accounts import sign_up
from tripl.tests._members import add_org_member
from tripl.tests._tenancy import use_multi_tenant
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branches import _seed_plan
from tripl.worker.tasks import maintenance

API = "/api/v1"
PASSWORD = "Password123!"
ORG_A_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
ORG_B_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _service_account(**extra: Any) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "alpha-project",
            "private_key_id": "k1",
            "private_key": pem,
            "client_email": "tripl@alpha-project.iam.gserviceaccount.com",
            "client_id": "1",
            "token_uri": "https://oauth2.googleapis.com/token",
            **extra,
        }
    )


SERVICE_ACCOUNT = _service_account()


def _variant(**changes: Any) -> str:
    """SERVICE_ACCOUNT with some fields changed (no second RSA key to generate)."""
    return json.dumps({**json.loads(SERVICE_ACCOUNT), **changes})


class FakeGcs(photo_storage.PhotoStorage):
    """Stands in for ``GCSPhotoStorage``: records what it was built with.

    ``listing`` is what a bucket lists; a driver built with a key whose
    ``client_email`` is in ``revoked`` cannot list; ``deleted`` records deletes.
    """

    backend_name = "gcs"
    built: list[dict[str, Any]] = []
    listing: dict[str, list[StoredObject]] = {}
    revoked: set[str] = set()
    deleted: list[str] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.objects: dict[str, bytes] = {}
        FakeGcs.built.append(kwargs)

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        FakeGcs.deleted.append(key)

    def delete_blocking(self, key: str) -> None:
        FakeGcs.deleted.append(key)

    def list_objects(self, prefix: str) -> Iterator[StoredObject]:
        info = self.kwargs.get("credentials_info") or {}
        if info.get("client_email") in FakeGcs.revoked:
            raise PermissionError("the key was revoked")
        bucket = self.kwargs["bucket_name"]
        return iter([obj for obj in FakeGcs.listing.get(bucket, []) if obj.key.startswith(prefix)])

    async def read(self, key: str) -> bytes:
        return self.objects[key]

    async def public_url(self, key: str, content_type: str) -> str | None:
        return f"https://storage.googleapis.com/{self.kwargs['bucket_name']}/{key}"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    monkeypatch.setattr(settings, "photo_storage_backend", "local")
    monkeypatch.setattr(settings, "photo_local_dir", str(tmp_path))
    monkeypatch.setattr(settings, "photo_max_size_mb", 10)
    monkeypatch.setattr(settings, "gcs_photo_bucket", "")
    monkeypatch.setattr(photo_storage, "GCSPhotoStorage", FakeGcs)
    FakeGcs.built = []
    FakeGcs.listing = {}
    FakeGcs.revoked = set()
    FakeGcs.deleted = []
    reset_photo_storage()
    photo_storage_service.forget_cached_versions()
    org_storage_csp.reset()
    yield tmp_path
    reset_photo_storage()
    photo_storage_service.forget_cached_versions()
    org_storage_csp.reset()
    crypto._fernet.cache_clear()


def _gcs_org(**extra: Any) -> dict[str, Any]:
    return {
        "photo_storage_backend": "gcs",
        "gcs_photo_bucket": "alpha-photos",
        "gcs_photo_credentials_json": crypto.encrypt_value(SERVICE_ACCOUNT),
        **extra,
    }


# ── resolution (pure) ───────────────────────────────────────────────────────


def test_an_org_without_storage_of_its_own_uses_the_operators_under_its_prefix() -> None:
    resolved = resolve_settings({}, {}, org_scope=ORG_A_ID)
    policy = photo_storage_service.policy_for(resolved, ORG_A_ID)
    assert policy.storage.owner_org_id is None
    assert policy.storage.backend == "local"
    assert policy.key_prefix == f"orgs/{ORG_A_ID}/events/"
    assert policy.max_size_mb == 10


def test_the_storage_group_ignores_the_fallback_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Photos off is not a safe default: an org without storage keeps the operator's."""
    monkeypatch.setattr(settings, "org_settings_operator_fallback", "none")
    resolved = resolve_settings({}, {}, org_scope=ORG_A_ID)
    assert resolved.sources["photo_storage_backend"] != "disabled"
    assert photo_storage_service.policy_for(resolved, ORG_A_ID).storage.backend == "local"


def test_an_orgs_own_bucket_is_one_group_with_its_own_credentials() -> None:
    resolved = resolve_settings(
        {"gcs_photo_bucket": "operator-bucket"}, _gcs_org(), org_scope=ORG_A_ID
    )
    policy = photo_storage_service.policy_for(resolved, ORG_A_ID)
    assert policy.storage.owner_org_id == ORG_A_ID
    assert policy.storage.bucket == "alpha-photos"
    assert policy.storage.credentials_json == SERVICE_ACCOUNT
    # Setting the bucket alone makes the group the org's: the unset members take
    # the built-in defaults, never the operator's.
    only_bucket = resolve_settings(
        {"gcs_photo_public": True}, {"gcs_photo_bucket": "x-photos"}, org_scope=ORG_A_ID
    )
    assert only_bucket.values["gcs_photo_public"] is False
    assert only_bucket.values["gcs_photo_credentials_json"] == ""
    assert only_bucket.sources["gcs_photo_public"] == "default"


def test_the_size_cap_is_clamped_and_the_types_narrowed() -> None:
    resolved = resolve_settings(
        {"photo_max_size_mb": 8, "photo_allowed_mime": "image/png,image/jpeg"},
        {"photo_max_size_mb": 50, "photo_allowed_mime": "image/svg+xml, IMAGE/PNG"},
        org_scope=ORG_A_ID,
    )
    assert resolved.values["photo_max_size_mb"] == 8
    assert resolved.values["photo_allowed_mime"] == "image/png"
    policy = photo_storage_service.policy_for(resolved, ORG_A_ID)
    # And never above what this process enforces as the body limit.
    assert policy.max_size_mb == 8
    assert policy.allowed_mime == ("image/png",)


# ── drivers ─────────────────────────────────────────────────────────────────


def test_org_drivers_are_cached_per_org_and_fingerprint() -> None:
    one = StorageConfig(ORG_A_ID, "gcs", "alpha-photos", credentials_json=SERVICE_ACCOUNT)
    same = StorageConfig(ORG_A_ID, "gcs", "alpha-photos", credentials_json=SERVICE_ACCOUNT)
    public = StorageConfig(
        ORG_A_ID, "gcs", "alpha-photos", public=True, credentials_json=SERVICE_ACCOUNT
    )
    assert driver_for_config(one) is driver_for_config(same)
    assert driver_for_config(public) is not driver_for_config(one)
    assert one.fingerprint() != public.fingerprint()
    # Built with the organization's key, never a server path.
    assert FakeGcs.built[0]["credentials_info"]["client_email"].startswith("tripl@")
    assert "credentials_path" not in FakeGcs.built[0]


def test_an_org_bucket_without_its_own_credentials_is_never_built() -> None:
    with pytest.raises(MissingStorageCredentials):
        driver_for_config(StorageConfig(ORG_A_ID, "gcs", "alpha-photos"))


def test_an_org_key_only_ever_talks_to_googles_token_endpoint() -> None:
    """A stored key's token_uri is overwritten at build time (rows saved before the check)."""
    stored = _variant(token_uri="http://169.254.169.254/latest/meta-data/")
    driver_for_config(StorageConfig(ORG_A_ID, "gcs", "alpha-photos", credentials_json=stored))
    assert FakeGcs.built[-1]["credentials_info"]["token_uri"] == GOOGLE_TOKEN_URI
    other_universe = _variant(universe_domain="evil.example")
    with pytest.raises(UnsafeServiceAccount):
        driver_for_config(
            StorageConfig(ORG_A_ID, "gcs", "alpha-photos", credentials_json=other_universe)
        )


async def test_a_storage_version_is_written_once_and_read_back() -> None:
    async with TestSessionLocal() as session:
        session.add(Organization(id=ORG_A_ID, slug="alpha", name="Alpha"))
        await session.commit()
        config = StorageConfig(ORG_A_ID, "gcs", "alpha-photos", credentials_json=SERVICE_ACCOUNT)
        first = await photo_storage_service.ensure_config_row(session, config)
        again = await photo_storage_service.ensure_config_row(session, config)
        await session.commit()
        assert first is not None and first == again
        row = await session.get(PhotoStorageConfig, first)
        assert row is not None
        # Encrypted at rest.
        assert SERVICE_ACCOUNT not in json.dumps(row.value)
    photo_storage_service.forget_cached_versions()
    async with TestSessionLocal() as session:
        assert await photo_storage_service.version_config(session, first) == config
        driver = await photo_storage_service.driver_for_blob(session, "gcs", first)
        assert isinstance(driver, FakeGcs)
        with pytest.raises(photo_storage_service.UnknownStorageVersion):
            await photo_storage_service.driver_for_blob(session, "gcs", uuid.uuid4())
        assert (
            await photo_storage_service.ensure_config_row(
                session, photo_storage_service.operator_policy().storage
            )
            is None
        )


# ── the organization settings API ───────────────────────────────────────────


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _register(client: AsyncClient, name: str) -> uuid.UUID:
    # Hosted: a verified default-org member, as hosted sign-up used to make.
    return await sign_up(client, email=f"{name}@example.com", password=PASSWORD, name=name)


async def _move_to_org(user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> None:
    async with TestSessionLocal() as session:
        await session.execute(
            delete(OrganizationMember).where(
                OrganizationMember.user_id == user_id,
                OrganizationMember.organization_id == DEFAULT_ORG_ID,
            )
        )
        await session.commit()
        await add_org_member(session, user_id, role, org_id=org_id)


class Hosted:
    def __init__(self) -> None:
        self.operator = _new_client()
        self.a_admin = _new_client()
        self.a_member = _new_client()


@pytest.fixture
async def hosted(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Hosted]:
    use_multi_tenant(monkeypatch)
    # The operator's cap as saved AND as this process runs it (storage
    # overrides are applied at startup).
    monkeypatch.setattr(settings, "photo_max_size_mb", 8)
    async with TestSessionLocal() as session:
        session.add_all(
            [
                Organization(id=ORG_A_ID, slug="alpha", name="Alpha"),
                Organization(id=ORG_B_ID, slug="bravo", name="Bravo"),
            ]
        )
        session.add(
            AppSetting(
                key=SERVICE_SETTINGS_KEY,
                value={"gcs_photo_bucket": "operator-secret-bucket", "photo_max_size_mb": 8},
                organization_id=None,
            )
        )
        await session.commit()
    h = Hosted()
    try:
        operator_id = await _register(h.operator, "operator")
        a_admin = await _register(h.a_admin, "alpha-admin")
        a_member = await _register(h.a_member, "alpha-member")
        async with TestSessionLocal() as session:
            op = await session.get(User, operator_id)
            assert op is not None
            op.is_platform_admin = True
            await session.commit()
        await _move_to_org(operator_id, ORG_B_ID, "owner")
        await _move_to_org(a_admin, ORG_A_ID, "admin")
        await _move_to_org(a_member, ORG_A_ID, "member")
        yield h
    finally:
        for client in (h.operator, h.a_admin, h.a_member):
            await client.aclose()


async def test_an_org_sets_its_own_bucket_and_the_key_stays_write_only(hosted: Hosted) -> None:
    before = await hosted.a_admin.get(f"{API}/orgs/alpha/settings")
    assert before.status_code == 200, before.text
    body = before.json()
    # Inheriting: told it uses the platform's storage, not which bucket.
    assert body["storage"]["gcs_photo_bucket"] == ""
    assert body["inherited"]["storage"]["gcs_photo_bucket"] == ""
    assert "operator-secret-bucket" not in before.text
    assert body["ceilings"]["photo_max_size_mb"] == 8
    assert body["storage_limits"]["local_backend_allowed"] is False
    assert "image/png" in body["storage_limits"]["operator_allowed_mime"]

    resp = await hosted.a_admin.patch(
        f"{API}/orgs/alpha/settings",
        json={
            "storage": {
                "photo_storage_backend": "gcs",
                "gcs_photo_bucket": "alpha-photos",
                "gcs_photo_credentials_json": SERVICE_ACCOUNT,
                "photo_allowed_mime": "image/png",
                "photo_max_size_mb": 5,
            }
        },
    )
    assert resp.status_code == 200, resp.text
    storage = resp.json()["storage"]
    assert storage["gcs_photo_bucket"] == "alpha-photos"
    assert storage["gcs_photo_credentials_configured"] is True
    assert "private_key" not in resp.text
    assert resp.json()["sources"]["storage.gcs_photo_bucket"] == "org"
    assert org_storage_csp.org_uses_gcs(org_id=ORG_A_ID)

    async with TestSessionLocal() as session:
        row = await session.scalar(
            select(AppSetting).where(
                AppSetting.organization_id == ORG_A_ID, AppSetting.key == SERVICE_SETTINGS_KEY
            )
        )
        assert row is not None
        assert "private_key" not in json.dumps(row.value)

    # Every member reads the organization's own limits.
    limits = await hosted.a_member.get(f"{API}/orgs/alpha/settings/photo-limits")
    assert limits.status_code == 200, limits.text
    assert limits.json() == {"photo_max_size_mb": 5, "photo_allowed_mime": ["image/png"]}
    # The other organization is untouched.
    bravo = await hosted.operator.get(f"{API}/orgs/bravo/settings/photo-limits")
    assert bravo.json()["photo_max_size_mb"] == 8


@pytest.mark.parametrize(
    ("storage", "fragment"),
    [
        ({"photo_storage_backend": "local"}, "local backend"),
        (
            {"photo_storage_backend": "gcs", "gcs_photo_bucket": "alpha-photos"},
            "service-account JSON",
        ),
        (
            {
                "photo_storage_backend": "gcs",
                "gcs_photo_bucket": "alpha-photos",
                "gcs_photo_credentials_json": "not json at all",
            },
            "service-account JSON key",
        ),
        (
            {
                "photo_storage_backend": "gcs",
                "gcs_photo_credentials_json": SERVICE_ACCOUNT,
            },
            "bucket",
        ),
        (
            {
                "photo_storage_backend": "gcs",
                "gcs_photo_bucket": "alpha-photos",
                "gcs_photo_credentials_json": _variant(token_uri="http://127.0.0.1/"),
            },
            "token_uri",
        ),
        (
            {
                "photo_storage_backend": "gcs",
                "gcs_photo_bucket": "alpha-photos",
                "gcs_photo_credentials_json": _variant(universe_domain="evil.example"),
            },
            "universe_domain",
        ),
        ({"photo_allowed_mime": "image/png,image/svg+xml"}, "image/svg+xml"),
        ({"photo_max_size_mb": 9}, "operator's limit"),
    ],
)
async def test_unusable_org_storage_is_refused(
    hosted: Hosted, storage: dict[str, Any], fragment: str
) -> None:
    resp = await hosted.a_admin.patch(f"{API}/orgs/alpha/settings", json={"storage": storage})
    assert resp.status_code == 422, resp.text
    assert fragment in resp.text
    assert "not json at all" not in resp.text


@pytest.mark.parametrize(
    "storage",
    [{"photo_local_dir": "/etc"}, {"gcs_photo_credentials_path": "/etc/shadow"}],
)
async def test_server_paths_are_not_organization_fields(
    hosted: Hosted, storage: dict[str, Any]
) -> None:
    resp = await hosted.a_admin.patch(f"{API}/orgs/alpha/settings", json={"storage": storage})
    assert resp.status_code == 422, resp.text


async def test_a_member_cannot_write_or_read_storage_settings(hosted: Hosted) -> None:
    assert (await hosted.a_member.get(f"{API}/orgs/alpha/settings")).status_code == 403
    assert (await hosted.a_admin.get(f"{API}/orgs/bravo/settings/photo-limits")).status_code == 404


async def test_self_hosted_default_org_storage_is_the_operators(client: AsyncClient) -> None:
    """The alias of the operator scope: its GCS key is a server path, not a setting."""
    resp = await client.patch(
        f"{API}/orgs/default/settings",
        json={"storage": {"gcs_photo_credentials_json": SERVICE_ACCOUNT}},
    )
    assert resp.status_code == 422, resp.text
    assert "GCS_PHOTO_CREDENTIALS_PATH" in resp.text


async def test_self_hosted_org_admin_cannot_raise_the_operator_ceiling(
    client: AsyncClient,
) -> None:
    admin = _new_client()
    try:
        admin_id = await _register(admin, "defaultadmin")
        async with TestSessionLocal() as session:
            await session.execute(
                delete(OrganizationMember).where(OrganizationMember.user_id == admin_id)
            )
            await session.commit()
            await add_org_member(session, admin_id, "admin")
        for storage in ({"photo_max_size_mb": 500}, {"gcs_photo_bucket": "elsewhere"}):
            resp = await admin.patch(f"{API}/orgs/default/settings", json={"storage": storage})
            assert resp.status_code == 403, resp.text
    finally:
        await admin.aclose()


# ── uploads, reads and versions ─────────────────────────────────────────────


async def _main_event_id(client: AsyncClient, slug: str) -> str:
    events = await client.get(f"{API}/projects/{slug}/events")
    return str(events.json()["items"][0]["id"])


async def _upload(client: AsyncClient, slug: str, event_id: str) -> Any:
    return await client.post(
        f"{API}/projects/{slug}/events/{event_id}/photos",
        files={"file": ("shot.png", _PNG, "image/png")},
    )


async def _set_default_org_storage(value: dict[str, Any]) -> None:
    async with TestSessionLocal() as session:
        await session.execute(
            delete(AppSetting).where(AppSetting.organization_id == DEFAULT_ORG_ID)
        )
        session.add(
            AppSetting(key=SERVICE_SETTINGS_KEY, value=value, organization_id=DEFAULT_ORG_ID)
        )
        await session.commit()


async def test_new_keys_carry_the_org_prefix_on_the_operators_store(
    client: AsyncClient, _env: Path
) -> None:
    slug = "org-prefix"
    await _seed_plan(client, slug)
    event_id = await _main_event_id(client, slug)
    uploaded = await _upload(client, slug, event_id)
    assert uploaded.status_code == 201, uploaded.text
    async with TestSessionLocal() as session:
        photo = await session.get(EventPhoto, uuid.UUID(uploaded.json()["id"]))
        assert photo is not None
        assert photo.storage_key is not None
        assert photo.storage_key.startswith(f"orgs/{DEFAULT_ORG_ID}/events/{event_id}/")
        assert photo.storage_org_id == DEFAULT_ORG_ID
        assert photo.storage_config_id is None
    assert (_env / photo.storage_key).read_bytes() == _PNG


async def test_a_photo_is_read_with_the_storage_version_it_was_written_with(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The organization moves to another bucket; its older photo still loads."""
    slug = "org-versions"
    await _seed_plan(client, slug)
    event_id = await _main_event_id(client, slug)
    # Hosted: the default organization is a tenant with its own scope.
    use_multi_tenant(monkeypatch)
    await _set_default_org_storage(_gcs_org())
    first = await _upload(client, slug, event_id)
    assert first.status_code == 201, first.text
    assert first.json()["url"].startswith("https://storage.googleapis.com/alpha-photos/orgs/")

    await _set_default_org_storage(_gcs_org(gcs_photo_bucket="alpha-photos-2"))
    second = await _upload(client, slug, event_id)
    assert second.status_code == 201, second.text
    assert "/alpha-photos-2/" in second.json()["url"]

    listed = await client.get(f"{API}/projects/{slug}/events/{event_id}/photos")
    urls = {item["id"]: item["url"] for item in listed.json()}
    assert "/alpha-photos/" in urls[first.json()["id"]]
    assert "/alpha-photos-2/" in urls[second.json()["id"]]
    async with TestSessionLocal() as session:
        rows = (await session.execute(select(EventPhoto))).scalars().all()
        assert len({row.storage_config_id for row in rows}) == 2
        versions = (await session.execute(select(PhotoStorageConfig))).scalars().all()
        assert {v.organization_id for v in versions} == {DEFAULT_ORG_ID}

    # The blob is deleted through the version that holds it.
    old_driver = next(
        d
        for d in photo_storage._BY_CONFIG.values()
        if getattr(d, "kwargs", {}).get("bucket_name") == "alpha-photos"
    )
    assert isinstance(old_driver, FakeGcs)
    assert len(old_driver.objects) == 1
    deleted = await client.delete(
        f"{API}/projects/{slug}/events/{event_id}/photos/{first.json()['id']}"
    )
    assert deleted.status_code == 204, deleted.text
    assert old_driver.objects == {}


async def test_the_csp_keeps_gcs_while_older_photos_are_read_from_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back on the operator's local store, a photo written to the org's bucket still loads."""
    slug = "org-csp-history"
    await _seed_plan(client, slug)
    event_id = await _main_event_id(client, slug)
    use_multi_tenant(monkeypatch)
    await _set_default_org_storage(_gcs_org())
    uploaded = await _upload(client, slug, event_id)
    assert uploaded.status_code == 201, uploaded.text

    await _set_default_org_storage({})
    listed = await client.get(f"{API}/projects/{slug}/events/{event_id}/photos")
    assert listed.json()[0]["url"].startswith("https://storage.googleapis.com/")
    # The write-through after the save that cleared the storage group...
    async with TestSessionLocal() as session:
        await org_settings_service.note_storage_for_csp(session, DEFAULT_ORG_ID)
    assert org_storage_csp.org_uses_gcs(org_id=DEFAULT_ORG_ID)
    # ...and the periodic reload another process relies on.
    monkeypatch.setattr(database, "async_session", TestSessionLocal)
    assert DEFAULT_ORG_ID in await org_storage_csp.load_from_database()
    org_storage_csp.reset()
    assert security_headers.GCS_IMAGE_ORIGIN in await _csp(_csp_app(monkeypatch), "/o/default/")


async def test_the_upload_obeys_the_orgs_narrowed_types(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = "org-types"
    await _seed_plan(client, slug)
    event_id = await _main_event_id(client, slug)
    use_multi_tenant(monkeypatch)
    await _set_default_org_storage({"photo_allowed_mime": "image/jpeg"})
    refused = await _upload(client, slug, event_id)
    assert refused.status_code == 415, refused.text


# ── the orphan sweep ────────────────────────────────────────────────────────


def _write(root: Path, key: str) -> Path:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    stamp = time.time() - 48 * 3600
    os.utime(path, (stamp, stamp))
    return path


def test_the_sweep_lists_only_prefixes_tripl_writes(
    _env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'sweep.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(maintenance, "_get_sync_session", factory)
    monkeypatch.setattr(settings, "photo_orphan_sweep_grace_hours", 24)
    event_id = uuid.uuid4()
    kept_key = f"orgs/{ORG_A_ID}/events/{event_id}/kept.png"
    kept = _write(_env, kept_key)
    orphan = _write(_env, f"orgs/{ORG_A_ID}/events/{event_id}/orphan.png")
    unknown_org = _write(_env, f"orgs/{uuid.uuid4()}/events/{event_id}/x.png")
    foreign = _write(_env, "orgs/notes.txt")
    session: Session
    with factory() as session:
        session.add(Organization(id=ORG_A_ID, slug="alpha", name="Alpha"))
        session.add(
            EventPhoto(
                id=uuid.uuid4(),
                project_id=uuid.uuid4(),
                event_id=event_id,
                storage_backend="local",
                storage_key=kept_key,
                storage_org_id=ORG_A_ID,
            )
        )
        session.commit()
    try:
        result = maintenance.sweep_orphan_photo_blobs()
    finally:
        engine.dispose()
    assert kept.exists()
    assert not orphan.exists()
    # Not a prefix tripl wrote for any organization it knows of.
    assert unknown_org.exists()
    assert foreign.exists()
    assert result["deleted"] == [f"local:orgs/{ORG_A_ID}/events/{event_id}/orphan.png"]


def _old(key: str) -> StoredObject:
    return StoredObject(key=key, written_at=datetime.now(UTC) - timedelta(days=3))


def test_the_sweep_lists_an_org_bucket_with_its_newest_key(
    _env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older version of the same bucket holds a revoked key; the sweep still runs."""
    engine = create_engine(f"sqlite:///{tmp_path / 'sweep.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(maintenance, "_get_sync_session", factory)
    monkeypatch.setattr(settings, "photo_orphan_sweep_grace_hours", 24)
    revoked = _variant(client_email="old@alpha-project.iam.gserviceaccount.com")
    FakeGcs.revoked = {"old@alpha-project.iam.gserviceaccount.com"}
    prefix = f"orgs/{ORG_A_ID}/events/{uuid.uuid4()}/"
    FakeGcs.listing = {"alpha-photos": [_old(prefix + "kept.png"), _old(prefix + "orphan.png")]}
    old_id, new_id = uuid.uuid4(), uuid.uuid4()
    session: Session
    with factory() as session:
        session.add(Organization(id=ORG_A_ID, slug="alpha", name="Alpha"))
        session.flush()
        # The revoked version first, as an unordered scan would return it.
        for version_id, key, when in (
            (old_id, revoked, datetime(2026, 1, 1, tzinfo=UTC)),
            (new_id, SERVICE_ACCOUNT, datetime(2026, 6, 1, tzinfo=UTC)),
        ):
            config = StorageConfig(ORG_A_ID, "gcs", "alpha-photos", credentials_json=key)
            session.add(
                PhotoStorageConfig(
                    id=version_id,
                    organization_id=ORG_A_ID,
                    config_hash=config.fingerprint(),
                    backend="gcs",
                    value=photo_storage_service._row_value(config),
                    created_at=when,
                )
            )
            session.flush()
        session.add(
            EventPhoto(
                id=uuid.uuid4(),
                project_id=uuid.uuid4(),
                event_id=uuid.uuid4(),
                storage_backend="gcs",
                storage_key=prefix + "kept.png",
                storage_org_id=ORG_A_ID,
                storage_config_id=old_id,
            )
        )
        session.commit()
    try:
        result = maintenance.sweep_orphan_photo_blobs()
    finally:
        engine.dispose()
    assert result["skipped_backends"] == []
    assert result["deleted"] == [f"org:{ORG_A_ID}:gcs:{prefix}orphan.png"]
    assert FakeGcs.deleted == [prefix + "orphan.png"]


# ── organization deletion ───────────────────────────────────────────────────


async def test_the_purge_deletes_blobs_through_the_orgs_own_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, uuid.UUID | None]] = []

    class Recorder:
        async def delete(self, key: str) -> None:
            return None

    async def fake_driver(_session: Any, backend: str, config_id: uuid.UUID | None) -> Recorder:
        seen.append((backend, config_id))
        return Recorder()

    monkeypatch.setattr(org_deletion_service, "driver_for_blob", fake_driver)
    monkeypatch.setattr(org_deletion_service, "storage_for", lambda _b: Recorder())
    version = uuid.uuid4()
    async with TestSessionLocal() as session:
        deleted, failed = await org_deletion_service._delete_blobs(
            session, {("gcs", "orgs/a/events/e/1.png", version), ("local", "events/e/2.png", None)}
        )
    assert (deleted, failed) == (2, 0)
    # The organization's version through its own driver; the operator's by name.
    assert seen == [("gcs", version)]


async def test_the_purge_keeps_the_org_until_its_prefix_is_empty(_env: Path) -> None:
    """A blob the purge cannot delete keeps the organization, so a later run retries it."""
    prefix = f"orgs/{ORG_A_ID}/events/{uuid.uuid4()}/"
    on_disk = _write(_env, prefix + "left.png")
    elsewhere = _write(_env, f"orgs/{ORG_B_ID}/events/{uuid.uuid4()}/other.png")
    FakeGcs.listing = {"alpha-photos": [_old(prefix + "in-bucket.png")]}
    FakeGcs.revoked = {"tripl@alpha-project.iam.gserviceaccount.com"}
    async with TestSessionLocal() as session:
        session.add(
            Organization(
                id=ORG_A_ID,
                slug="alpha",
                name="Alpha",
                status=OrganizationStatus.deleting.value,
            )
        )
        await session.commit()
        config = StorageConfig(ORG_A_ID, "gcs", "alpha-photos", credentials_json=SERVICE_ACCOUNT)
        assert await photo_storage_service.ensure_config_row(session, config) is not None
        await session.commit()

        first = await org_deletion_service.purge_organization(session, ORG_A_ID)
        assert first is not None
        assert not first.complete
        assert first.leftovers_failed == 1
        # The operator's store is cleared under this organization's prefix only.
        assert not on_disk.exists()
        assert elsewhere.exists()
        session.expire_all()
        kept = await session.get(Organization, ORG_A_ID)
        assert kept is not None and kept.status == OrganizationStatus.deleting.value
        assert await session.scalar(select(PhotoStorageConfig.id)) is not None

        FakeGcs.revoked = set()
        second = await org_deletion_service.purge_organization(session, ORG_A_ID)
        assert second is not None and second.complete
        assert FakeGcs.deleted == [prefix + "in-bucket.png"]
        session.expire_all()
        assert await session.get(Organization, ORG_A_ID) is None
        assert await session.scalar(select(PhotoStorageConfig.id)) is None


# ── CSP per request ─────────────────────────────────────────────────────────


def _csp_app(monkeypatch: pytest.MonkeyPatch) -> security_headers.SecurityHeadersMiddleware:
    monkeypatch.setattr(settings, "security_headers_enabled", True)
    monkeypatch.setattr(settings, "serve_frontend", True)
    monkeypatch.setattr(settings, "content_security_policy", "")

    async def ok(_request: Any) -> PlainTextResponse:
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/{path:path}", ok)])
    return security_headers.SecurityHeadersMiddleware(inner)


async def _csp(asgi: Any, path: str) -> str:
    # No organization bound, as for a request the auth dependency has not
    # resolved (the suite binds the default organization around each test).
    token = bind_org(None)
    try:
        async with AsyncClient(transport=ASGITransport(app=asgi), base_url="http://t") as http:
            resp = await http.get(path)
    finally:
        reset_org(token)
    return resp.headers["content-security-policy"]


async def test_the_csp_admits_gcs_only_where_an_org_uses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asgi = _csp_app(monkeypatch)

    async def load() -> dict[uuid.UUID, str]:
        return {ORG_A_ID: "alpha"}

    monkeypatch.setattr(org_storage_csp, "loader", load)
    gcs = security_headers.GCS_IMAGE_ORIGIN
    assert gcs in await _csp(asgi, "/o/alpha/p/web")
    assert gcs in await _csp(asgi, f"{API}/orgs/alpha/projects/web/events")
    assert gcs not in await _csp(asgi, f"{API}/orgs/bravo/projects/web/events")
    # A page of the SPA: another organization's photos may show without a reload.
    assert gcs in await _csp(asgi, "/o/bravo/p/web")

    # A request the auth dependency bound to an organization is answered for it.
    with bound_org(OrgRef(id=ORG_A_ID, slug="alpha")):
        async with AsyncClient(transport=ASGITransport(app=asgi), base_url="http://t") as http:
            bound = await http.get(f"{API}/projects/web/events")
    assert gcs in bound.headers["content-security-policy"]

    org_storage_csp.note_org_backend(ORG_A_ID, "alpha", "local")
    assert gcs not in await _csp(asgi, "/o/alpha/p/web")


async def test_a_failed_registry_load_never_fails_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asgi = _csp_app(monkeypatch)

    async def broken() -> dict[uuid.UUID, str]:
        raise RuntimeError("database down")

    monkeypatch.setattr(org_storage_csp, "loader", broken)
    assert security_headers.GCS_IMAGE_ORIGIN not in await _csp(asgi, "/o/alpha/")
