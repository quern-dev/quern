"""Build records (#326, step 2): what quern built, kept past the next build.

No Xcode runs here: bundles are made of synthetic Mach-O bytes, and `dsymutil`
is a fake that writes a dSYM-shaped directory. Measured once by hand on a real
Geocaching device build: `dsymutil` on its 117 MB .debug.dylib takes 5.4 s and
gives a 160 MB dSYM, and `atos` against that dSYM resolves the phone's crash
frame to `AppDelegate.swift:13`.
"""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from server.device import build_records
from server.models import BuildBinary, BuildRecord, BuildResult
from tests.test_macho import thin

U_APP = uuid.UUID("8078f7c1-dc60-38a9-ba72-a17ec2727331")
U_MAIN = uuid.UUID("cc8131b8-ffdb-3d26-b4c3-29cf2541b833")
U_VENDOR = uuid.UUID("6c8a7761-3000-338f-8ac4-9bb9f477df6e")
U_PLAIN = uuid.UUID("c59d7ec6-fa95-3629-b040-4f91f013198e")
NOW = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)


def _app(tmp_path: Path, name="MyApp") -> Path:
    """A built app: its own code (a debug map naming objects that exist), a
    vendored framework with a map naming its vendor's machine, one with no map,
    a resource, and a symlink."""
    objects = tmp_path / "DerivedData" / "Intermediates"
    objects.mkdir(parents=True)
    (objects / "AppDelegate.o").write_bytes(b"o")
    products = tmp_path / "DerivedData" / "Products" / "Debug-iphoneos"
    app = products / f"{name}.app"
    (app / "Frameworks" / "Vendor.framework").mkdir(parents=True)
    (app / "Frameworks" / "Plain.framework").mkdir(parents=True)
    with open(app / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": "com.example.myapp",
                       "CFBundleShortVersionString": "1.2.3", "CFBundleVersion": "42"}, f)
    own = (str(objects / "AppDelegate.o"),)
    (app / f"{name}.debug.dylib").write_bytes(thin(U_APP, objects=own))
    (app / name).write_bytes(thin(U_MAIN))
    (app / "Frameworks" / "Vendor.framework" / "Vendor").write_bytes(
        thin(U_VENDOR, objects=("/data/sandcastle/boxes/vendor/Vendor.o",)))
    (app / "Frameworks" / "Plain.framework" / "Plain").write_bytes(thin(U_PLAIN))
    (app / "Assets.car").write_bytes(b"not a binary")
    (app / "Frameworks" / "Plain.framework" / "Current").symlink_to("Plain")
    return app


def _dsym(where: Path, name: str, *uuids: uuid.UUID) -> Path:
    """A dSYM bundle whose DWARF files carry `uuids`."""
    dwarf = where / name / "Contents" / "Resources" / "DWARF"
    dwarf.mkdir(parents=True)
    for i, u in enumerate(uuids):
        (dwarf / f"bin{i}").write_bytes(thin(u))
    return where / name


class FakeDsymutil:
    def __init__(self, code=0, err="", raises=None):
        self.calls: list[list[str]] = []
        self.code, self.err, self.raises = code, err, raises

    async def __call__(self, argv):
        self.calls.append(argv)
        if self.raises:
            raise self.raises
        out = Path(argv[argv.index("-o") + 1])
        (out / "Contents" / "Resources" / "DWARF").mkdir(parents=True)
        return self.code, self.err

    @property
    def inputs(self):
        return [Path(a[2]).name for a in self.calls]


def _record(app, root, run, platform="iphoneos", **kw):
    return asyncio.run(build_records.record_build(
        app, project_path="/src/MyApp.xcworkspace", scheme=kw.pop("scheme", "MyApp"),
        configuration="Debug", platform=platform, root=root, run=run, now=kw.pop("now", NOW)))


class TestADeviceBuild:
    def test_what_was_built_is_recorded(self, tmp_path):
        record = _record(_app(tmp_path), tmp_path / "records", FakeDsymutil())
        assert (record.bundle_id, record.version, record.build_number) == (
            "com.example.myapp", "1.2.3", "42")
        by_path = {b.path: b for b in record.binaries}
        assert set(by_path) == {"MyApp.debug.dylib", "MyApp", "Frameworks/Vendor.framework/Vendor",
                                "Frameworks/Plain.framework/Plain"}
        assert by_path["MyApp.debug.dylib"].uuids == {"arm64": str(U_APP).upper()}
        assert record.error == ""

    def test_only_code_built_here_gets_a_dsym(self, tmp_path):
        """The vendored framework's map names its vendor's machine; the plain
        one has none. Neither is handed to dsymutil."""
        run = FakeDsymutil()
        record = _record(_app(tmp_path), tmp_path / "records", run)
        assert run.inputs == ["MyApp.debug.dylib"]
        kept = {b.path: b.dsym for b in record.binaries if b.dsym}
        assert list(kept) == ["MyApp.debug.dylib"]
        assert kept["MyApp.debug.dylib"] == str(
            tmp_path / "records" / record.build_id / "dSYMs" / "MyApp.debug.dylib.dSYM")
        assert Path(kept["MyApp.debug.dylib"]).is_dir()

    def test_it_is_written_whole_or_not_at_all(self, tmp_path):
        root = tmp_path / "records"
        record = _record(_app(tmp_path), root, FakeDsymutil())
        assert [p.name for p in root.iterdir()] == [record.build_id]
        path = root / record.build_id / "record.json"
        on_disk = BuildRecord.model_validate_json(path.read_text())
        assert on_disk == record

    def test_a_dsym_xcode_made_is_copied_once_for_every_binary_it_covers(self, tmp_path):
        """`MyApp.app.dSYM` holds the app and its .debug.dylib: copying it per
        binary doubled a real record to 371 MB."""
        app = _app(tmp_path)
        _dsym(app.parent, "MyApp.app.dSYM", U_APP, U_MAIN)
        run = FakeDsymutil()
        record = _record(app, tmp_path / "records", run)
        assert run.calls == []
        dsyms = {b.path: b.dsym for b in record.binaries if b.dsym}
        assert dsyms["MyApp.debug.dylib"] == dsyms["MyApp"]
        assert Path(dsyms["MyApp"]).name == "MyApp.app.dSYM"
        assert [p.name for p in (tmp_path / "records" / record.build_id / "dSYMs").iterdir()] == [
            "MyApp.app.dSYM"]

    def test_a_vendored_frameworks_own_dsym_is_kept(self, tmp_path):
        app = _app(tmp_path)
        _dsym(app.parent, "Vendor.framework.dSYM", U_VENDOR)
        record = _record(app, tmp_path / "records", FakeDsymutil())
        vendor = next(b for b in record.binaries if b.path.endswith("Vendor"))
        assert Path(vendor.dsym).name == "Vendor.framework.dSYM"

    def test_a_stale_dsym_is_not_mistaken_for_this_build(self, tmp_path):
        app = _app(tmp_path)
        _dsym(app.parent, "MyApp.app.dSYM", uuid.uuid4())       # an earlier build's
        run = FakeDsymutil()
        _record(app, tmp_path / "records", run)
        assert run.inputs == ["MyApp.debug.dylib"]


class TestWhenSymbolsCannotBeKept:
    """Said on the record, never raised: the build installed."""

    @pytest.mark.parametrize("run, says", [
        (FakeDsymutil(code=1, err="error: unable to write\n"),
         "dsymutil exited 1: error: unable to write"),
        (FakeDsymutil(raises=FileNotFoundError(2, "No such file", "xcrun")), "No such file"),
        (FakeDsymutil(raises=TimeoutError()), "TimeoutError"),
    ])
    def test_a_failed_dsymutil(self, tmp_path, run, says):
        record = _record(_app(tmp_path), tmp_path / "records", run)
        app_bin = next(b for b in record.binaries if b.path == "MyApp.debug.dylib")
        assert app_bin.dsym == "" and says in app_bin.dsym_error
        dsyms = tmp_path / "records" / record.build_id / "dSYMs"
        assert not (dsyms / "MyApp.debug.dylib.dSYM").exists()
        assert "not for MyApp.debug.dylib" in build_records.summary_line(record)

    def test_warnings_keep_the_dsym_and_say_it_may_be_incomplete(self, tmp_path):
        run = FakeDsymutil(err="warning: (arm64) /dd/A.o unable to open object file\n")
        record = _record(_app(tmp_path), tmp_path / "records", run)
        app_bin = next(b for b in record.binaries if b.path == "MyApp.debug.dylib")
        assert app_bin.dsym and app_bin.dsym_warnings == [
            "warning: (arm64) /dd/A.o unable to open object file"]
        assert "may be incomplete" in build_records.summary_line(record)

    def test_an_unwritable_records_directory(self, tmp_path):
        root = tmp_path / "records"
        root.write_text("a file where the directory should be")
        record = _record(_app(tmp_path), root, FakeDsymutil())
        assert "could not be written" in record.error
        assert all(b.dsym == "" for b in record.binaries)
        assert "could not be written" in build_records.summary_line(record)

    def test_no_info_plist(self, tmp_path):
        app = _app(tmp_path)
        (app / "Info.plist").unlink()
        record = _record(app, tmp_path / "records", FakeDsymutil())
        assert record.bundle_id == "" and "Info.plist could not be read" in record.notes[0]
        assert record.binaries


class TestASimulatorBuild:
    def test_the_record_alone(self, tmp_path):
        """macOS writes file and line into a simulator's crash report."""
        run = FakeDsymutil()
        record = _record(_app(tmp_path), tmp_path / "records", run, platform="iphonesimulator")
        assert run.calls == [] and all(b.dsym == "" for b in record.binaries)
        assert (tmp_path / "records" / record.build_id / "record.json").exists()
        assert "4 binaries' UUIDs" in build_records.summary_line(record)


def _stored(root: Path, n: int, *, scheme="MyApp", platform="iphoneos", age=timedelta(0)):
    """A record already on disk, with a dSYM when it is a device build."""
    created = NOW - age - timedelta(minutes=n)
    bid = f"{created:%Y%m%d-%H%M%S}-{platform}-{n:06d}"
    d = root / bid
    dsym = ""
    if platform == "iphoneos":
        (d / "dSYMs" / "MyApp.dSYM").mkdir(parents=True)
        dsym = str(d / "dSYMs" / "MyApp.dSYM")
    d.mkdir(parents=True, exist_ok=True)
    record = BuildRecord(build_id=bid, created_at=created, project_path="/src/p", scheme=scheme,
                         configuration="Debug", platform=platform, app_path="/x",
                         binaries=[BuildBinary(path="MyApp", dsym=dsym)])
    (d / "record.json").write_text(record.model_dump_json())
    return record


class TestRetention:
    def test_dsyms_beyond_the_newest_ten_of_a_scheme_expire(self, tmp_path):
        root = tmp_path / "records"
        mine = [_stored(root, n) for n in range(12)]            # n=0 is the newest
        theirs = [_stored(root, 100 + n, scheme="Other") for n in range(2)]
        build_records.prune(root, now=NOW)
        records = {r.build_id: r for r in build_records.load_all(root)}
        for r in mine[:10] + theirs:
            assert (root / r.build_id / "dSYMs").is_dir() and not records[r.build_id].dsyms_expired
        for r in mine[10:]:
            assert not (root / r.build_id / "dSYMs").exists()
            assert records[r.build_id].dsyms_expired
            assert records[r.build_id].binaries[0].dsym == ""

    def test_simulator_records_do_not_use_up_the_ten(self, tmp_path):
        root = tmp_path / "records"
        for n in range(15):
            _stored(root, n, platform="iphonesimulator")
        device = [_stored(root, 100 + n) for n in range(10)]
        build_records.prune(root, now=NOW)
        assert all((root / r.build_id / "dSYMs").is_dir() for r in device)

    def test_records_older_than_thirty_days_go(self, tmp_path):
        root = tmp_path / "records"
        old = _stored(root, 1, age=timedelta(days=31))
        recent = _stored(root, 2, age=timedelta(days=29))
        build_records.prune(root, now=NOW)
        assert not (root / old.build_id).exists() and (root / recent.build_id).exists()

    def test_an_abandoned_partial_goes_and_a_fresh_one_stays(self, tmp_path):
        root = tmp_path / "records"
        stale, fresh = root / "a.partial", root / "b.partial"
        stale.mkdir(parents=True)
        fresh.mkdir()
        two_hours_ago = NOW.timestamp() - 7200
        os.utime(stale, (two_hours_ago, two_hours_ago))
        os.utime(fresh, (NOW.timestamp(), NOW.timestamp()))
        build_records.prune(root, now=NOW)
        assert not stale.exists() and fresh.exists()

    def test_an_unreadable_record_is_skipped_not_fatal(self, tmp_path):
        root = tmp_path / "records"
        good = _stored(root, 1)
        (root / "broken").mkdir()
        (root / "broken" / "record.json").write_text("{not json")
        assert [r.build_id for r in build_records.load_all(root)] == [good.build_id]
        build_records.prune(root, now=NOW)


# ── the route ────────────────────────────────────────────────────────────────

HEADERS = {"Authorization": "Bearer test-key-12345"}


class FakeController:
    def __init__(self, kinds):
        from server.models import DeviceType
        self.kinds = {u: DeviceType(k) for u, k in kinds.items()}
        self.installed: list[tuple[str, str]] = []
        self.wda_client = SimpleNamespace(_device_os_versions={})

        async def boot(udid):
            return None
        self.simctl = SimpleNamespace(boot=boot)

    async def resolve_udid(self, udid):
        return udid

    def _device_type(self, udid):
        return self.kinds.get(udid)

    async def install_app(self, app_path, udid):
        self.installed.append((app_path, udid))


@pytest.fixture
def build_app(tmp_path, monkeypatch):
    from server.api import build_app as route
    from server.config import ServerConfig
    from server.main import create_app

    app = create_app(config=ServerConfig(api_key="test-key-12345"),
                     enable_oslog=False, enable_crash=False, enable_proxy=False)
    app.state.build_adapter = object()
    project = tmp_path / "src" / "MyApp.xcworkspace"
    project.mkdir(parents=True)
    built = _app(tmp_path)
    run = FakeDsymutil()

    async def fake_build(*args, **kwargs):
        return BuildResult(succeeded=True)

    monkeypatch.setattr(route, "_build", fake_build)
    monkeypatch.setattr(route, "_find_app", lambda derived, config, physical: built)
    monkeypatch.setattr(route, "CONFIG_DIR", tmp_path / "state")
    monkeypatch.setattr(build_records, "RECORDS_DIR", tmp_path / "records")
    monkeypatch.setattr(build_records, "_run", run)
    return SimpleNamespace(app=app, project=project, records=tmp_path / "records", run=run)


async def _build_and_install(app, **body):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/v1/device/build-and-install", headers=HEADERS, json=body)


class TestTheRoute:
    async def test_a_device_build_is_recorded_and_named_in_the_summary(self, build_app):
        build_app.app.state.device_controller = FakeController({"00008101-PHONE": "device"})
        resp = await _build_and_install(build_app.app, project_path=str(build_app.project),
                                        scheme="MyApp", udids=["00008101-PHONE"])
        assert resp.status_code == 200, resp.text
        data = resp.json()
        [record] = data["build_records"]
        assert record["platform"] == "iphoneos" and record["installed_on"] == ["00008101-PHONE"]
        assert record["bundle_id"] == "com.example.myapp"
        # The MCP tool shows only the summary on success.
        assert f"Recorded build {record['build_id']}" in data["summary"]
        assert "symbols kept for 1 binary" in data["summary"]
        on_disk = json.loads((build_app.records / record["build_id"] / "record.json").read_text())
        assert on_disk["installed_on"] == ["00008101-PHONE"]

    async def test_a_simulator_build_is_recorded_without_dsyms(self, build_app):
        build_app.app.state.device_controller = FakeController({"SIM-UDID": "simulator"})
        resp = await _build_and_install(build_app.app, project_path=str(build_app.project),
                                        scheme="MyApp", udids=["SIM-UDID"])
        [record] = resp.json()["build_records"]
        assert record["platform"] == "iphonesimulator" and record["installed_on"] == ["SIM-UDID"]
        assert build_app.run.calls == []

    async def test_a_recording_defect_does_not_fail_the_install(self, build_app, monkeypatch):
        async def broken(*args, **kwargs):
            raise KeyError("boom")

        monkeypatch.setattr(build_records, "record_build", broken)
        ctrl = FakeController({"00008101-PHONE": "device"})
        build_app.app.state.device_controller = ctrl
        resp = await _build_and_install(build_app.app, project_path=str(build_app.project),
                                        scheme="MyApp", udids=["00008101-PHONE"])
        data = resp.json()
        assert resp.status_code == 200 and data["all_installed"] and ctrl.installed
        [record] = data["build_records"]
        assert "could not be recorded: KeyError" in record["error"]
        assert "could not be recorded" in data["summary"]

    async def test_a_failed_install_is_not_listed_as_installed_on(self, build_app):
        ctrl = FakeController({"00008101-PHONE": "device"})

        async def refuse(app_path, udid):
            from server.models import DeviceError
            raise DeviceError("no room")

        ctrl.install_app = refuse
        build_app.app.state.device_controller = ctrl
        resp = await _build_and_install(build_app.app, project_path=str(build_app.project),
                                        scheme="MyApp", udids=["00008101-PHONE"])
        [record] = resp.json()["build_records"]
        assert record["installed_on"] == [] and record["error"] == ""
