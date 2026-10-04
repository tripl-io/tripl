import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_project_anomaly_settings_creates_defaults(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Monitoring Project", "slug": "monitoring-project", "description": ""},
    )
    assert project_resp.status_code == 201

    resp = await client.get("/api/v1/projects/monitoring-project/anomaly-settings")

    assert resp.status_code == 200
    body = resp.json()
    assert body["anomaly_detection_enabled"] is False
    assert body["detect_project_total"] is True
    assert body["baseline_window_buckets"] == 14
    assert body["min_history_buckets"] == 7
    assert body["sigma_threshold"] == 4.0
    assert body["min_expected_count"] == 50
    # The open-signal freshness window must default to the historical 24h so
    # nothing changes for a project that never touches the setting.
    assert body["recent_signal_window_hours"] == 24


@pytest.mark.asyncio
async def test_update_project_anomaly_settings(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Monitoring Update", "slug": "monitoring-update", "description": ""},
    )
    assert project_resp.status_code == 201

    resp = await client.patch(
        "/api/v1/projects/monitoring-update/anomaly-settings",
        json={
            "anomaly_detection_enabled": True,
            "detect_events": False,
            "baseline_window_buckets": 21,
            "min_history_buckets": 9,
            "sigma_threshold": 4.5,
            "min_expected_count": 25,
            "recent_signal_window_hours": 6,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["anomaly_detection_enabled"] is True
    assert body["detect_events"] is False
    assert body["baseline_window_buckets"] == 21
    assert body["min_history_buckets"] == 9
    assert body["sigma_threshold"] == 4.5
    assert body["min_expected_count"] == 25
    assert body["recent_signal_window_hours"] == 6

    read_back = await client.get("/api/v1/projects/monitoring-update/anomaly-settings")
    assert read_back.status_code == 200
    assert read_back.json()["recent_signal_window_hours"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [0, 721])
async def test_recent_signal_window_hours_out_of_range_is_rejected(
    client: AsyncClient, hours: int
) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": f"Window {hours}", "slug": f"window-{hours}", "description": ""},
    )
    assert project_resp.status_code == 201

    resp = await client.patch(
        f"/api/v1/projects/window-{hours}/anomaly-settings",
        json={"recent_signal_window_hours": hours},
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingestion_settling_minutes_defaults_and_updates(client: AsyncClient) -> None:
    """the ingestion-settling allowance is a per-project setting.

    Its default must reproduce the module constant it replaced (2 hours), so a
    project that never touches it keeps today's detection latency.
    """
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Settling Project", "slug": "settling-project", "description": ""},
    )
    assert project_resp.status_code == 201

    defaults = await client.get("/api/v1/projects/settling-project/anomaly-settings")
    assert defaults.status_code == 200
    assert defaults.json()["anomaly_ingestion_settling_minutes"] == 120

    updated = await client.patch(
        "/api/v1/projects/settling-project/anomaly-settings",
        json={"anomaly_ingestion_settling_minutes": 45},
    )
    assert updated.status_code == 200
    assert updated.json()["anomaly_ingestion_settling_minutes"] == 45

    read_back = await client.get("/api/v1/projects/settling-project/anomaly-settings")
    assert read_back.status_code == 200
    assert read_back.json()["anomaly_ingestion_settling_minutes"] == 45


@pytest.mark.asyncio
@pytest.mark.parametrize(("label", "minutes"), [("negative", -1), ("too-long", 1441)])
async def test_ingestion_settling_minutes_out_of_range_is_rejected(
    client: AsyncClient, label: str, minutes: int
) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": f"Settling {label}", "slug": f"settling-{label}", "description": ""},
    )
    assert project_resp.status_code == 201

    resp = await client.patch(
        f"/api/v1/projects/settling-{label}/anomaly-settings",
        json={"anomaly_ingestion_settling_minutes": minutes},
    )

    assert resp.status_code == 422


class TestSettlingAllowanceVersusOpenSignalWindow:
    """the two dials must not be set to cancel each other out.

    A bucket is held back from scoring for ``anomaly_ingestion_settling_minutes``
    and then counts as an open signal for ``recent_signal_window_hours``. Once
    the allowance reaches the window, the newest anomaly the detector may emit is
    already outside the window on every grid the window governs, so the Anomalies
    page, the sidebar badge and the Overview stat all read zero — while alerting,
    which classifies against the settled head, keeps delivering. The pair is
    refused rather than clamped: both numbers are operator-set, and silently
    rewriting whichever one was touched last makes the stored result depend on
    edit order.
    """

    async def _project(self, client: AsyncClient, slug: str) -> None:
        resp = await client.post(
            "/api/v1/projects",
            json={"name": slug, "slug": slug, "description": ""},
        )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_a_conflicting_pair_in_one_patch_is_refused(self, client: AsyncClient) -> None:
        await self._project(client, "settling-pair")

        resp = await client.patch(
            "/api/v1/projects/settling-pair/anomaly-settings",
            json={
                "anomaly_ingestion_settling_minutes": 1440,
                "recent_signal_window_hours": 24,
            },
        )

        assert resp.status_code == 422
        assert "open signal window" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_raising_the_allowance_into_the_stored_window_is_refused(
        self, client: AsyncClient
    ) -> None:
        """Direction one: the window is already stored, the allowance moves up."""
        await self._project(client, "settling-raise")
        # Stored window: 24h. Left untouched by the patch below.
        assert (await client.get("/api/v1/projects/settling-raise/anomaly-settings")).json()[
            "recent_signal_window_hours"
        ] == 24

        resp = await client.patch(
            "/api/v1/projects/settling-raise/anomaly-settings",
            json={"anomaly_ingestion_settling_minutes": 1440},
        )

        assert resp.status_code == 422
        # The stored value must be untouched — a refused patch writes nothing.
        read_back = await client.get("/api/v1/projects/settling-raise/anomaly-settings")
        assert read_back.json()["anomaly_ingestion_settling_minutes"] == 120

    @pytest.mark.asyncio
    async def test_lowering_the_window_onto_the_stored_allowance_is_refused(
        self, client: AsyncClient
    ) -> None:
        """Direction two, the one a body-only check cannot see: the allowance is
        already stored (120 by default) and the window drops under it."""
        await self._project(client, "settling-lower")

        resp = await client.patch(
            "/api/v1/projects/settling-lower/anomaly-settings",
            json={"recent_signal_window_hours": 2},
        )

        assert resp.status_code == 422
        read_back = await client.get("/api/v1/projects/settling-lower/anomaly-settings")
        assert read_back.json()["recent_signal_window_hours"] == 24

    @pytest.mark.asyncio
    async def test_an_allowance_one_minute_short_of_the_window_is_accepted(
        self, client: AsyncClient
    ) -> None:
        """The guard refuses only the collision, not every large allowance."""
        await self._project(client, "settling-edge")

        resp = await client.patch(
            "/api/v1/projects/settling-edge/anomaly-settings",
            json={"anomaly_ingestion_settling_minutes": 1439},
        )

        assert resp.status_code == 200
        assert resp.json()["anomaly_ingestion_settling_minutes"] == 1439

    @pytest.mark.asyncio
    async def test_a_window_raised_in_the_same_patch_makes_a_long_allowance_legal(
        self, client: AsyncClient
    ) -> None:
        """The check reads the MERGED settings, not one field against a default:
        a 24h allowance is fine as soon as signals stay open for 48h."""
        await self._project(client, "settling-together")

        resp = await client.patch(
            "/api/v1/projects/settling-together/anomaly-settings",
            json={
                "anomaly_ingestion_settling_minutes": 1440,
                "recent_signal_window_hours": 48,
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["anomaly_ingestion_settling_minutes"] == 1440
        assert body["recent_signal_window_hours"] == 48

    @pytest.mark.asyncio
    async def test_a_stored_conflict_does_not_brick_the_rest_of_the_form(
        self, client: AsyncClient
    ) -> None:
        """A row written before this guard existed still conflicts, and the guard
        only refuses patches that LEAVE it conflicting — otherwise one legacy row
        would lock every other detection setting on that project."""
        from sqlalchemy import select

        from tripl.models.project_anomaly_settings import ProjectAnomalySettings
        from tripl.tests.conftest import TestSessionLocal

        await self._project(client, "settling-legacy")
        # GET materialises the settings row, then write the illegal pair straight
        # to the DB the way a pre-guard PATCH would have.
        assert (
            await client.get("/api/v1/projects/settling-legacy/anomaly-settings")
        ).status_code == 200
        async with TestSessionLocal() as session:
            stored = await session.scalar(select(ProjectAnomalySettings))
            assert stored is not None
            stored.anomaly_ingestion_settling_minutes = 1440
            stored.recent_signal_window_hours = 24
            await session.commit()

        # Unrelated field: allowed, the conflict is neither created nor worsened.
        untouched = await client.patch(
            "/api/v1/projects/settling-legacy/anomaly-settings",
            json={"sigma_threshold": 5.0},
        )
        assert untouched.status_code == 200
        assert untouched.json()["sigma_threshold"] == 5.0

        # Resolving it is allowed too.
        resolved = await client.patch(
            "/api/v1/projects/settling-legacy/anomaly-settings",
            json={"recent_signal_window_hours": 48},
        )
        assert resolved.status_code == 200
        assert resolved.json()["recent_signal_window_hours"] == 48


@pytest.mark.asyncio
async def test_scope_overrides_are_listed_and_can_be_undone(client: AsyncClient) -> None:
    """The false-positive ratchet is permanent and does not decay, so Detection
    settings is the ONLY way back: it must list every tightened scope and let an
    operator delete one, dropping that scope back to the project setting."""
    import uuid

    from tripl.models.anomaly_scope_override import AnomalyScopeOverride
    from tripl.tests.conftest import TestSessionLocal

    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Undo Ratchet", "slug": "undo-ratchet", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    scope_ref = str(uuid.uuid4())
    async with TestSessionLocal() as session:
        session.add(
            AnomalyScopeOverride(
                project_id=project_id,
                scan_config_id=None,
                scope_type="metric",
                scope_ref=scope_ref,
                scope_name="Checkout conversion",
                sigma_threshold=4.5,
                min_expected_count=55,
                false_positive_count=1,
            )
        )
        await session.commit()

    listed = await client.get("/api/v1/projects/undo-ratchet/anomaly-settings/scope-overrides")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["scope_type"] == "metric"
    assert item["scope_ref"] == scope_ref
    assert item["scope_name"] == "Checkout conversion"
    assert item["scan_config_id"] is None  # metric scopes are project-global
    assert item["sigma_threshold"] == 4.5
    assert item["min_expected_count"] == 55
    assert item["false_positive_count"] == 1

    deleted = await client.delete(
        f"/api/v1/projects/undo-ratchet/anomaly-settings/scope-overrides/{item['id']}"
    )
    assert deleted.status_code == 204

    after = await client.get("/api/v1/projects/undo-ratchet/anomaly-settings/scope-overrides")
    assert after.status_code == 200
    assert after.json() == {"items": [], "total": 0}


@pytest.mark.asyncio
async def test_scope_override_from_another_project_is_not_deletable(client: AsyncClient) -> None:
    """The override id is a bare UUID in the path; without the project check any
    member of any project could reset another project's tuning."""
    import uuid

    from tripl.models.anomaly_scope_override import AnomalyScopeOverride
    from tripl.tests.conftest import TestSessionLocal

    owner = await client.post(
        "/api/v1/projects",
        json={"name": "Ratchet Owner", "slug": "ratchet-owner", "description": ""},
    )
    other = await client.post(
        "/api/v1/projects",
        json={"name": "Ratchet Other", "slug": "ratchet-other", "description": ""},
    )
    assert owner.status_code == 201
    assert other.status_code == 201
    owner_id = uuid.UUID(owner.json()["id"])

    async with TestSessionLocal() as session:
        override = AnomalyScopeOverride(
            project_id=owner_id,
            scan_config_id=None,
            scope_type="metric",
            scope_ref=str(uuid.uuid4()),
            scope_name="Owned metric",
            sigma_threshold=4.5,
            min_expected_count=55,
            false_positive_count=1,
        )
        session.add(override)
        await session.commit()
        override_id = override.id

    resp = await client.delete(
        f"/api/v1/projects/ratchet-other/anomaly-settings/scope-overrides/{override_id}"
    )
    assert resp.status_code == 404
