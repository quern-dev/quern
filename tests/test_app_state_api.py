"""Integration tests for the app state API endpoints."""

from __future__ import annotations

import plistlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device import plist as plist_module
from server.device.controller import DeviceController
from server.main import create_app
from server.models import AppStateNotFoundError, DeviceError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    config = ServerConfig(api_key="test-key-12345")
    return create_app(config=config, enable_oslog=False, enable_crash=False, enable_proxy=False)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key-12345"}


@pytest.fixture(autouse=True)
def syncs(monkeypatch):
    """Stand in for the cfprefsd restart, recording when it ran.

    Tests append their own markers to the same list, so ordering between the
    restarts and the edit can be asserted.
    """
    calls: list[str] = []
    result = {"synced": True}

    async def fake(udid):
        calls.append("sync")
        return dict(result)

    monkeypatch.setattr("server.api.app_state.sync_preferences", fake)
    fake.calls = calls
    fake.result = result
    return fake


@pytest.fixture
def container(tmp_path):
    """A real data container with a real preferences plist, resolved for every route."""
    root = tmp_path / "container"
    prefs = root / "Library" / "Preferences"
    prefs.mkdir(parents=True)
    plist = prefs / "com.example.App.plist"
    plist.write_bytes(plistlib.dumps({"existing": 1}, fmt=plistlib.FMT_BINARY))
    with patch("server.api.app_state.resolve_container", AsyncMock(return_value=root)):
        yield plist


PLIST = "Library/Preferences/com.example.App.plist"


async def _call(app, auth_headers, method, path, **kw):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(
            method, f"/api/v1/device/app/state{path}", headers=auth_headers, **kw,
        )


@pytest.fixture
def mock_controller(app):
    ctrl = MagicMock(spec=DeviceController)
    ctrl.resolve_udid = AsyncMock(return_value="AAAA-1111")
    ctrl._require_simulator = MagicMock()
    ctrl._is_physical = MagicMock(return_value=False)
    app.state.device_controller = ctrl
    return ctrl


# ---------------------------------------------------------------------------
# Checkpoint endpoints
# ---------------------------------------------------------------------------


class TestSaveEndpoint:
    async def test_save_endpoint(self, app, auth_headers, mock_controller):
        meta = {
            "label": "baseline",
            "bundle_id": "com.example.App",
            "captured_at": "2026-01-01T00:00:00+00:00",
            "udid": "AAAA-1111",
            "description": "",
            "containers": {"data": "/fake/path", "groups": {}},
        }
        with patch("server.api.app_state.save_state", AsyncMock(return_value=meta)):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/device/app/state/save",
                    json={"bundle_id": "com.example.App", "label": "baseline"},
                    headers=auth_headers,
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "saved"
        assert data["meta"]["label"] == "baseline"

    async def test_save_endpoint_simulator_only(self, app, auth_headers, mock_controller):
        mock_controller._require_simulator = MagicMock(
            side_effect=DeviceError("save_app_state is only supported on simulators", tool="simctl")
        )
        with patch("server.api.app_state.save_state", AsyncMock()):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/device/app/state/save",
                    json={"bundle_id": "com.example.App", "label": "baseline"},
                    headers=auth_headers,
                )
        # 400, not 500. This asserted 500 and so pinned the defect as
        # expected behaviour: asking for a simulator-only operation on
        # another kind of device is a bad request, and `server/api/device.py`
        # classified it that way all along. `app_state.py` carries its own
        # copy of `_handle_device_error` which never gained the rule, so the
        # same refusal came back 400 from one route and 500 from another
        # (#263). The test's own name says what it is about -- the refusal --
        # and the status was incidental to that.
        assert resp.status_code == 400


class TestRestoreEndpoint:
    async def test_restore_endpoint(self, app, auth_headers, mock_controller):
        meta = {
            "label": "baseline",
            "bundle_id": "com.example.App",
            "captured_at": "2026-01-01T00:00:00+00:00",
            "udid": "AAAA-1111",
            "description": "",
            "containers": {"data": "/fake/path", "groups": {}},
        }
        with patch("server.api.app_state.restore_state", AsyncMock(return_value=meta)):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/device/app/state/restore",
                    json={"bundle_id": "com.example.App", "label": "baseline"},
                    headers=auth_headers,
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "restored"

    async def test_restore_not_found(self, app, auth_headers, mock_controller):
        with patch(
            "server.api.app_state.restore_state",
            AsyncMock(
                side_effect=AppStateNotFoundError(
                    "Checkpoint 'x' not found for com.example.App", tool="simctl"
                )
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/device/app/state/restore",
                    json={"bundle_id": "com.example.App", "label": "x"},
                    headers=auth_headers,
                )
        assert resp.status_code == 404


class TestListEndpoint:
    async def test_list_endpoint(self, app, auth_headers, mock_controller):
        states = [
            {
                "label": "alpha",
                "bundle_id": "com.example.App",
                "captured_at": "2026-01-01T00:00:00+00:00",
            },
        ]
        with patch("server.api.app_state.list_states", return_value=states):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/device/app/state/list",
                    params={"bundle_id": "com.example.App"},
                    headers=auth_headers,
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["states"][0]["label"] == "alpha"


# ---------------------------------------------------------------------------
# Plist endpoints
# ---------------------------------------------------------------------------


class TestReadPlistEndpoint:
    async def test_read_plist_whole(self, app, auth_headers, mock_controller, container):
        resp = await _call(app, auth_headers, "GET", "/plist", params={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"] == {"existing": 1}

    async def test_read_plist_single_key(self, app, auth_headers, mock_controller, container):
        resp = await _call(app, auth_headers, "GET", "/plist", params={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "key": "existing",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["key"] == "existing" and resp.json()["value"] == 1

    async def test_a_read_flushes_cfprefsd_first(
        self, app, auth_headers, mock_controller, container, syncs,
    ):
        """The file lags the app by seconds until cfprefsd writes it out."""
        resp = await _call(app, auth_headers, "GET", "/plist", params={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
        })
        assert resp.status_code == 200
        assert syncs.calls == ["sync"]

    @pytest.mark.parametrize("bad", ["../../../../etc/x.plist", "/etc/hosts"])
    async def test_a_plist_path_outside_the_container_is_400(
        self, app, auth_headers, mock_controller, container, bad,
    ):
        resp = await _call(app, auth_headers, "GET", "/plist", params={
            "bundle_id": "com.example.App", "container": "data", "plist_path": bad,
        })
        assert resp.status_code == 400, resp.text


class TestSetPlistValueEndpoint:
    async def test_set_plist_value(self, app, auth_headers, mock_controller, container):
        resp = await _call(app, auth_headers, "POST", "/plist", json={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "key": "myFlag", "value": False,
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ok" and data["key"] == "myFlag" and data["value"] is False
        assert plistlib.loads(container.read_bytes()) == {"existing": 1, "myFlag": False}

    async def test_a_dotted_key_is_written_as_one_key(
        self, app, auth_headers, mock_controller, container,
    ):
        """The reverse-DNS key plutil could not write (`Key path not found`)."""
        resp = await _call(app, auth_headers, "POST", "/plist", json={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "key": "probe.greeting", "value": "hi",
        })
        assert resp.status_code == 200, resp.text
        assert plistlib.loads(container.read_bytes())["probe.greeting"] == "hi"

    async def test_cfprefsd_is_restarted_before_and_after_the_write(
        self, app, auth_headers, mock_controller, container, syncs,
    ):
        """Before, so a pending flush cannot land on top of the edit; after, so
        the cache stops serving the old value."""
        async def recording(path, values):
            syncs.calls.append("write")
            await plist_module.set_plist_values(path, values)

        with patch("server.api.app_state.set_plist_values", recording):
            resp = await _call(app, auth_headers, "POST", "/plist", json={
                "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
                "key": "k", "value": 1,
            })
        assert resp.status_code == 200, resp.text
        assert syncs.calls == ["sync", "write", "sync"]

    async def test_a_failed_sync_is_a_warning_on_the_response(
        self, app, auth_headers, mock_controller, container, syncs,
    ):
        syncs.result.clear()
        syncs.result.update({"synced": False, "detail": "x", "warning": "stale, beware"})
        resp = await _call(app, auth_headers, "POST", "/plist", json={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "key": "k", "value": 1,
        })
        assert resp.status_code == 200
        assert resp.json()["warning"] == "stale, beware"
        assert resp.json()["preferences"]["synced"] is False

    async def test_a_failed_first_sync_is_reported_when_the_second_succeeds(
        self, app, auth_headers, mock_controller, container, monkeypatch,
    ):
        results = iter([
            {"synced": False, "detail": "x", "warning": "flush failed"},
            {"synced": True},
        ])

        async def fake(udid):
            return next(results)

        monkeypatch.setattr("server.api.app_state.sync_preferences", fake)
        resp = await _call(app, auth_headers, "POST", "/plist", json={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "key": "k", "value": 1,
        })
        assert resp.status_code == 200
        assert resp.json()["warning"] == "flush failed"

    async def test_a_plist_the_app_wrote_but_cfprefsd_had_not_saved_is_found(
        self, app, auth_headers, mock_controller, container, monkeypatch,
    ):
        """The file appears only when cfprefsd flushes it, so the existence
        check has to come after the sync, not before."""
        container.unlink()

        async def flush(udid):
            if not container.exists():
                container.write_bytes(plistlib.dumps({"from_app": 1}))
            return {"synced": True}

        monkeypatch.setattr("server.api.app_state.sync_preferences", flush)
        resp = await _call(app, auth_headers, "POST", "/plist", json={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "key": "k", "value": 2,
        })
        assert resp.status_code == 200, resp.text
        assert plistlib.loads(container.read_bytes()) == {"from_app": 1, "k": 2}

    @pytest.mark.parametrize("value", [None, [1, 2], {"x": True}])
    async def test_a_value_that_is_not_a_scalar_is_refused(
        self, app, auth_headers, mock_controller, container, value,
    ):
        """It used to be stored as its Python repr -- "None", "[1, 2]" -- with a 200."""
        before = container.read_bytes()
        resp = await _call(app, auth_headers, "POST", "/plist", json={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "key": "k", "value": value,
        })
        assert resp.status_code == 422, resp.text
        assert container.read_bytes() == before

    async def test_a_plist_path_outside_the_container_is_400_and_writes_nothing(
        self, app, auth_headers, mock_controller, container, tmp_path,
    ):
        outside = tmp_path / "outside.plist"
        outside.write_bytes(plistlib.dumps({}))
        before = outside.read_bytes()
        resp = await _call(app, auth_headers, "POST", "/plist", json={
            "bundle_id": "com.example.App", "container": "data",
            "plist_path": "../outside.plist", "key": "k", "value": 1,
        })
        assert resp.status_code == 400, resp.text
        assert outside.read_bytes() == before


class TestBatchEndpoint:
    async def test_every_key_is_written(self, app, auth_headers, mock_controller, container):
        resp = await _call(app, auth_headers, "POST", "/plist/batch", json={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "values": {"a.b": "s", "c.d": 7, "e.f": True},
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok" and resp.json()["keys_set"] == 3
        assert plistlib.loads(container.read_bytes()) == {
            "existing": 1, "a.b": "s", "c.d": 7, "e.f": True,
        }

    async def test_a_failed_batch_is_an_error_and_changes_nothing(
        self, app, auth_headers, mock_controller, container,
    ):
        """It used to answer 200 `partial` with `keys_set: 0`."""
        before = container.read_bytes()
        resp = await _call(app, auth_headers, "POST", "/plist/batch", json={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "values": {"fine": 1, "too_big": 2**70},
        })
        assert resp.status_code == 500, resp.text
        assert "partial" not in resp.text
        assert container.read_bytes() == before


class TestDeleteKeyEndpoint:
    async def test_a_dotted_key_is_removed(self, app, auth_headers, mock_controller, container):
        container.write_bytes(plistlib.dumps({"probe.counter": 3}))
        resp = await _call(app, auth_headers, "DELETE", "/plist/key", json={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "key": "probe.counter",
        })
        assert resp.status_code == 200, resp.text
        assert plistlib.loads(container.read_bytes()) == {}

    async def test_a_missing_key_is_404(self, app, auth_headers, mock_controller, container):
        resp = await _call(app, auth_headers, "DELETE", "/plist/key", json={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "key": "nope",
        })
        assert resp.status_code == 404, resp.text


class TestDiffEndpoint:
    @pytest.fixture
    def checkpoint(self, tmp_path, monkeypatch, container):
        import server.device.app_state as module

        root = tmp_path / "state" / "app-states"
        monkeypatch.setattr(module, "APP_STATES_DIR", root)
        saved = root / "com.example.App" / "base" / "data-container" / PLIST
        saved.parent.mkdir(parents=True)
        saved.write_bytes(plistlib.dumps({"existing": 0, "gone": 1}))
        return saved

    async def test_it_names_what_changed(
        self, app, auth_headers, mock_controller, container, checkpoint,
    ):
        container.write_bytes(plistlib.dumps({"existing": 1, "new.key": True}))
        resp = await _call(app, auth_headers, "GET", "/plist/diff", params={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "checkpoint_label": "base",
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["changed"] == {"existing": {"old": 0, "new": 1}}
        assert body["added"] == {"new.key": True}
        assert body["removed"] == {"gone": 1}

    @pytest.mark.parametrize("label, status", [("nope", 404), ("..", 400)])
    async def test_a_bad_checkpoint_is_refused_before_the_device_is_touched(
        self, app, auth_headers, mock_controller, container, checkpoint, syncs,
        label, status,
    ):
        resp = await _call(app, auth_headers, "GET", "/plist/diff", params={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "checkpoint_label": label,
        })
        assert resp.status_code == status, resp.text
        assert syncs.calls == [], "cfprefsd was restarted for a request that was refused"

    async def test_the_live_side_is_flushed_before_it_is_read(
        self, app, auth_headers, mock_controller, container, checkpoint, syncs,
    ):
        resp = await _call(app, auth_headers, "GET", "/plist/diff", params={
            "bundle_id": "com.example.App", "container": "data", "plist_path": PLIST,
            "checkpoint_label": "base",
        })
        assert resp.status_code == 200
        assert syncs.calls == ["sync"]


class TestErrorsAreClassifiedByType:
    async def test_a_failure_quoting_a_container_path_is_not_a_404(
        self, app, auth_headers, mock_controller,
    ):
        """Every simulator container path contains `Containers`, and the old
        handler turned any such message with "not found" in it into a 404."""
        message = (
            "editing /Users/x/Library/Developer/CoreSimulator/Devices/U/data/"
            "Containers/Data/Application/A/Library/Preferences/p.plist failed: "
            "Key path not found"
        )
        with patch(
            "server.api.app_state.restore_state",
            AsyncMock(side_effect=DeviceError(message, tool="plistlib")),
        ):
            resp = await _call(app, auth_headers, "POST", "/restore", json={
                "bundle_id": "com.example.App", "label": "x",
            })
        assert resp.status_code == 500, resp.text


class TestCheckpointNamesStayInTheStore:
    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        import server.device.app_state as module

        root = tmp_path / "state" / "app-states"
        root.mkdir(parents=True)
        monkeypatch.setattr(module, "APP_STATES_DIR", root)
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "precious.txt").write_text("keep me")
        return victim

    async def test_delete_with_a_climbing_bundle_id_is_400(
        self, app, auth_headers, mock_controller, store,
    ):
        """`DELETE /victim?bundle_id=../..` resolved to `tmp_path/victim`."""
        resp = await _call(
            app, auth_headers, "DELETE", "/victim", params={"bundle_id": "../.."},
        )
        assert resp.status_code == 400, resp.text
        assert (store / "precious.txt").read_text() == "keep me"

    async def test_list_with_a_climbing_bundle_id_is_400(
        self, app, auth_headers, mock_controller, store,
    ):
        resp = await _call(app, auth_headers, "GET", "/list", params={"bundle_id": ".."})
        assert resp.status_code == 400, resp.text

    async def test_save_with_a_climbing_bundle_id_is_400(
        self, app, auth_headers, mock_controller, store,
    ):
        with patch("server.device.app_state._terminate_app", AsyncMock()) as terminate:
            resp = await _call(app, auth_headers, "POST", "/save", json={
                "bundle_id": "../..", "label": "victim",
            })
        assert resp.status_code == 400, resp.text
        assert (store / "precious.txt").read_text() == "keep me"
        terminate.assert_not_called()


class TestWatchStartChecksThePath:
    async def test_a_plist_path_outside_the_container_is_400(
        self, app, auth_headers, mock_controller, container,
    ):
        resp = await _call(app, auth_headers, "POST", "/plist/watch/start", json={
            "bundle_id": "com.example.App", "container": "data",
            "plist_path": "../../../../etc/x.plist",
        })
        assert resp.status_code == 400, resp.text

    async def test_a_missing_plist_is_404_not_500(
        self, app, auth_headers, mock_controller, container,
    ):
        resp = await _call(app, auth_headers, "POST", "/plist/watch/start", json={
            "bundle_id": "com.example.App", "container": "data",
            "plist_path": "Library/Preferences/none.plist",
        })
        assert resp.status_code == 404, resp.text
