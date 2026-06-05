"""Tests for the /api/v1/system/* endpoints — update awareness + trigger."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.main import create_app


@pytest.fixture
def app():
    config = ServerConfig(api_key="test-key-12345")
    return create_app(
        config=config, enable_oslog=False, enable_crash=False, enable_proxy=False,
    )


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key-12345"}


# ---------------------------------------------------------------------------
# /update-status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_status_returns_defaults_when_never_checked(
    app, auth_headers, monkeypatch,
):
    """Fresh install, opted out, or first 24h before check fires —
    endpoint must still respond cleanly with sensible defaults."""
    monkeypatch.setattr(
        "server.api.system.read_update_info", lambda: None,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/system/update-status", headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()

    assert data["update_available"] is False
    assert data["current_version"] is None
    assert data["latest_version"] is None
    assert data["checked_at"] is None


@pytest.mark.asyncio
async def test_update_status_surfaces_persisted_record(
    app, auth_headers, monkeypatch,
):
    monkeypatch.setattr(
        "server.api.system.read_update_info",
        lambda: {
            "checked_at": "2026-06-05T19:00:00+00:00",
            "current_version": "0.13.4",
            "latest_version": "0.13.5",
            "update_available": True,
            "message": 'Update available — run "quern update" ...',
        },
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/system/update-status", headers=auth_headers,
        )
        data = resp.json()

    assert data["update_available"] is True
    assert data["current_version"] == "0.13.4"
    assert data["latest_version"] == "0.13.5"
    assert data["checked_at"] == "2026-06-05T19:00:00+00:00"
    assert "Update available" in data["message"]


# ---------------------------------------------------------------------------
# /update (trigger)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_trigger_launches_detached_child(
    app, auth_headers, monkeypatch,
):
    """POST /update must spawn a detached subprocess and return 202-ish
    immediately. We can't actually run `quern update` in a test, so mock
    Popen and verify the spawn shape."""
    fake_proc = MagicMock()
    fake_proc.pid = 99999

    with patch(
        "server.api.system.subprocess.Popen", return_value=fake_proc,
    ) as popen_mock:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/system/update", headers=auth_headers,
            )
            assert resp.status_code == 200
            data = resp.json()

    assert data["status"] == "launched"
    assert data["pid"] == 99999
    assert "restart" in data["note"].lower()

    # Verify the child was started detached (start_new_session=True).
    # Without this, killing the server would kill the updater mid-run.
    assert popen_mock.call_count == 1
    kwargs = popen_mock.call_args.kwargs
    assert kwargs["start_new_session"] is True
    # The command should be `python -m server update`.
    args = popen_mock.call_args.args[0]
    assert args[-2:] == ["-m", "server", "update"][-2:]


@pytest.mark.asyncio
async def test_update_trigger_reports_skipped_on_oserror(
    app, auth_headers, monkeypatch,
):
    """If Popen fails (e.g. the env is too locked-down), the endpoint
    should still respond with a useful error, not 500."""
    with patch(
        "server.api.system.subprocess.Popen",
        side_effect=OSError("permission denied"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/system/update", headers=auth_headers,
            )
            assert resp.status_code == 200
            data = resp.json()

    assert data["status"] == "skipped"
    assert data["pid"] is None
    assert "permission denied" in data["note"]


# ---------------------------------------------------------------------------
# /channel — get/put
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect ~/.quern/config.json to a temp dir so tests don't mutate
    the real user config."""
    monkeypatch.setattr("server.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        "server.config.USER_CONFIG_FILE", tmp_path / "config.json",
    )
    return tmp_path


@pytest.mark.asyncio
async def test_get_channel_returns_default_when_unset(
    app, auth_headers, isolated_config,
):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/system/channel", headers=auth_headers)
        data = resp.json()

    assert data["channel"] == "stable"
    assert data["release_branch"] == "release/stable"
    assert data["valid_channels"] == ["stable", "beta"]


@pytest.mark.asyncio
async def test_put_channel_persists_and_returns_new_state(
    app, auth_headers, isolated_config,
):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/system/channel",
            headers=auth_headers,
            json={"channel": "beta"},
        )
        assert resp.status_code == 200
        data = resp.json()

    assert data["channel"] == "beta"
    assert data["release_branch"] == "release/beta"

    # Confirm persistence: subsequent GET should return the same value.
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/system/channel", headers=auth_headers)
        assert resp.json()["channel"] == "beta"


@pytest.mark.asyncio
async def test_put_channel_rejects_unknown_channel(
    app, auth_headers, isolated_config,
):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/system/channel",
            headers=auth_headers,
            json={"channel": "nightly"},
        )

    assert resp.status_code == 400
    assert "Unknown update channel" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_status_includes_channel(
    app, auth_headers, isolated_config, monkeypatch,
):
    """update-status must surface the configured channel + release branch
    so MCP clients can render them inline without an extra round-trip."""
    monkeypatch.setattr(
        "server.api.system.read_update_info",
        lambda: {
            "checked_at": "2026-06-05T19:00:00+00:00",
            "current_version": "0.13.4",
            "latest_version": "0.13.5",
            "update_available": True,
            "message": "Update available",
        },
    )
    from server.config import set_update_channel
    set_update_channel("beta")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/system/update-status", headers=auth_headers,
        )
        data = resp.json()

    assert data["channel"] == "beta"
    assert data["release_branch"] == "release/beta"
    assert data["update_available"] is True
    assert data["latest_version"] == "0.13.5"
