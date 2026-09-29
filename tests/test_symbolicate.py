"""Symbolicating a device's crash against the build that crashed (#326, step 3).

No atos and no mdfind run here: both are a fake runner. Measured once for
real, on a phone crash forced through a Debug build's debug menu: the phone
named the crashing closure with no line, and atos against the build record's
DWARF put it at the fatalError's own line -- but only when looked up one byte
before the return address; at the address itself it said
`<compiler-generated>:0`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from server.models import BuildBinary, BuildRecord, CrashFrame, CrashImage, CrashReport
from server.sources import symbolicate
from tests.test_macho import thin

U_APP = uuid.UUID("ea0d22d8-97a1-3a5f-bd5f-039535e0d45b")
BASE = 0x10AB20000
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
APP_PATH = "/private/var/containers/Bundle/Application/X/MyApp.app/MyApp.debug.dylib"


def _report(*, frames_from="crashing_thread", app_image_path=APP_PATH, extra_frames=()):
    """A device crash: a system frame on top, then two of the app's with no line."""
    frames = [
        CrashFrame(image="libswiftCore.dylib", offset=100, symbol="_assertionFailure"),
        CrashFrame(image="MyApp.debug.dylib", offset=0x1000, symbol="closure #4 in Menu.tap()",
                   symbol_offset=252, app=True),
        CrashFrame(image="MyApp.debug.dylib", offset=0x2000, symbol="Cell.pressed()", app=True),
        *extra_frames,
    ]
    return CrashReport(
        crash_id="c1", timestamp=NOW, process="MyApp", frames=frames, frames_from=frames_from,
        app_frame=frames[1],
        images=[
            CrashImage(name="libswiftCore.dylib", uuid="aa" * 16, base=0x190000000,
                       path="/usr/lib/swift/libswiftCore.dylib", arch="arm64e"),
            CrashImage(name="MyApp.debug.dylib", uuid=str(U_APP), base=BASE,
                       path=app_image_path, arch="arm64"),
        ],
    )


def _dsym(where: Path, *uuids: uuid.UUID) -> Path:
    dwarf = where / "MyApp.app.dSYM" / "Contents" / "Resources" / "DWARF"
    dwarf.mkdir(parents=True)
    for i, u in enumerate(uuids):
        (dwarf / f"bin{i}").write_bytes(thin(u))
    return where / "MyApp.app.dSYM"


def _record(root: Path, dsym: Path, *, dwarf: str | None = None, expired=False) -> BuildRecord:
    record = BuildRecord(
        build_id="20260929-053143-iphoneos-daa674", created_at=NOW, project_path="/p",
        scheme="MyApp", configuration="Debug", platform="iphoneos", app_path="/a",
        dsyms_expired=expired,
        binaries=[BuildBinary(
            path="MyApp.debug.dylib", uuids={"arm64": str(U_APP).upper()},
            dsym="" if expired else str(dsym),
            dwarf="" if expired else (dwarf if dwarf is not None
                                     else str(dsym / "Contents/Resources/DWARF/bin0")))],
    )
    (root / record.build_id).mkdir(parents=True)
    (root / record.build_id / "record.json").write_text(record.model_dump_json())
    return record


class FakeTools:
    """mdfind and atos. atos answers each address from `lines`, keyed by the
    address it was asked for, so a lookup at the wrong address shows up."""

    def __init__(self, lines=None, mdfind="", mdfind_raises=None, atos_code=0, atos_extra=0):
        self.calls: list[list[str]] = []
        self.lines = lines or {}
        self.mdfind, self.mdfind_raises = mdfind, mdfind_raises
        self.atos_code, self.atos_extra = atos_code, atos_extra

    async def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "mdfind":
            if self.mdfind_raises:
                raise self.mdfind_raises
            return 0, self.mdfind, ""
        addresses = [int(a, 16) for a in argv[argv.index("-l") + 2:]]
        out = [self.lines.get(a, f"{a:#x} (in MyApp.debug.dylib)") for a in addresses]
        out += ["extra"] * self.atos_extra
        return self.atos_code, "\n".join(out) + "\n", "atos: boom" if self.atos_code else ""

    @property
    def atos(self):
        return [c for c in self.calls if c[:2] == ["xcrun", "atos"]]


def _run(reports, finder):
    asyncio.run(symbolicate.symbolicate_many(reports, finder))


CLOSURE_AT = BASE + 0x1000 - 1        # a return address: looked up one byte before
CELL_AT = BASE + 0x2000 - 1
GOOD = {
    CLOSURE_AT: "closure #4 in Menu.tap() (in MyApp.debug.dylib) (Menu.swift:170)",
    CELL_AT: "Cell.pressed() (in MyApp.debug.dylib) (Cell.swift:37)",
}


class TestFromABuildRecord:
    def test_the_apps_frames_get_their_lines(self, tmp_path):
        dsym = _dsym(tmp_path / "records-dsyms", U_APP)
        _record(tmp_path / "records", dsym)
        tools = FakeTools(lines=GOOD)
        report = _report()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", tools))
        lines = [(f.file, f.line) for f in report.frames[1:]]
        assert lines == [("Menu.swift", 170), ("Cell.swift", 37)]
        assert (report.app_frame.file, report.app_frame.line) == ("Menu.swift", 170)
        [entry] = report.symbols
        assert (entry.source, entry.build_id, entry.frames_resolved, entry.frames_total) == (
            "build_record", "20260929-053143-iphoneos-daa674", 2, 2)
        assert entry.note == ""
        assert "(Menu.swift:170)" in report.top_frames[1]

    def test_atos_gets_the_records_dwarf_and_the_images_load_address(self, tmp_path):
        dsym = _dsym(tmp_path / "d", U_APP)
        _record(tmp_path / "records", dsym)
        tools = FakeTools(lines=GOOD)
        _run([_report()], symbolicate.SymbolFinder(tmp_path / "records", tools))
        [argv] = tools.atos
        assert argv[:8] == ["xcrun", "atos", "-o", str(dsym / "Contents/Resources/DWARF/bin0"),
                            "-arch", "arm64", "-l", hex(BASE)]
        assert tools.calls[0][:2] == ["xcrun", "atos"]        # no Spotlight needed

    def test_a_record_kept_before_dwarf_existed_is_found_inside_its_dsym(self, tmp_path):
        """A real record made during testing had `dsym` and no `dwarf`."""
        dsym = _dsym(tmp_path / "d", uuid.uuid4(), U_APP)
        _record(tmp_path / "records", dsym, dwarf="")
        tools = FakeTools(lines=GOOD)
        _run([_report()], symbolicate.SymbolFinder(tmp_path / "records", tools))
        assert tools.atos[0][3] == str(dsym / "Contents/Resources/DWARF/bin1")


class TestReturnAddresses:
    def test_only_the_crashing_threads_top_frame_is_exact(self, tmp_path):
        dsym = _dsym(tmp_path / "d", U_APP)
        _record(tmp_path / "records", dsym)
        top = CrashFrame(image="MyApp.debug.dylib", offset=0x500, app=True)
        report = _report()
        report.frames.insert(0, top)
        tools = FakeTools(lines=GOOD)
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", tools))
        asked = [int(a, 16) for a in tools.atos[0][8:]]
        assert asked == sorted([BASE + 0x500, CLOSURE_AT, CELL_AT])

    def test_an_exception_backtrace_is_return_addresses_throughout(self, tmp_path):
        dsym = _dsym(tmp_path / "d", U_APP)
        _record(tmp_path / "records", dsym)
        report = _report(frames_from="exception")
        report.frames.insert(0, CrashFrame(image="MyApp.debug.dylib", offset=0x500, app=True))
        tools = FakeTools(lines=GOOD)
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", tools))
        assert BASE + 0x500 - 1 in [int(a, 16) for a in tools.atos[0][8:]]


class TestWhereElse:
    def test_spotlight_when_no_record_has_it(self, tmp_path):
        dsym = _dsym(tmp_path / "DerivedData", U_APP)
        tools = FakeTools(lines=GOOD, mdfind=f"{dsym}\n")
        report = _report()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", tools))
        assert tools.calls[0] == ["mdfind", f"com_apple_xcode_dsym_uuids == {str(U_APP).upper()}"]
        [entry] = report.symbols
        assert entry.source == "spotlight" and entry.build_id == ""
        assert report.app_frame.line == 170

    def test_an_expired_record_is_said_and_spotlight_still_asked(self, tmp_path):
        _record(tmp_path / "records", tmp_path / "gone", expired=True)
        tools = FakeTools(mdfind="")
        report = _report()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", tools))
        assert tools.calls[0][0] == "mdfind"
        assert "symbols were removed" in report.symbols[0].note

    def test_no_match_is_said_not_guessed(self, tmp_path):
        tools = FakeTools(mdfind="")
        report = _report()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", tools))
        [entry] = report.symbols
        assert entry.source == "" and "no symbols on this Mac" in entry.note
        assert str(U_APP).upper() in entry.note
        assert tools.atos == [] and report.app_frame.file == ""

    def test_spotlight_that_cannot_be_asked_is_said(self, tmp_path):
        tools = FakeTools(mdfind_raises=FileNotFoundError(2, "No such file", "mdfind"))
        report = _report()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", tools))
        assert "Spotlight could not be asked" in report.symbols[0].note


class TestWhenAtosFails:
    @pytest.mark.parametrize("tools, says", [
        (FakeTools(lines=GOOD, atos_code=1), "atos exited 1"),
        (FakeTools(lines=GOOD, atos_extra=1), "atos gave 3 lines for 2 addresses"),
    ])
    def test_it_is_said_and_the_frames_are_left(self, tmp_path, tools, says):
        dsym = _dsym(tmp_path / "d", U_APP)
        _record(tmp_path / "records", dsym)
        report = _report()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", tools))
        assert says in report.symbols[0].note
        assert report.app_frame.file == "" and report.symbols[0].frames_resolved == 0

    def test_atos_missing(self, tmp_path):
        dsym = _dsym(tmp_path / "d", U_APP)
        _record(tmp_path / "records", dsym)

        async def run(argv):
            raise FileNotFoundError(2, "No such file", "xcrun")

        report = _report()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", run))
        assert "atos could not run" in report.symbols[0].note


class TestHonestCounting:
    def test_a_compiler_generated_frame_is_not_counted_as_resolved(self, tmp_path):
        dsym = _dsym(tmp_path / "d", U_APP)
        _record(tmp_path / "records", dsym)
        lines = dict(GOOD)
        lines[CELL_AT] = "Cell.pressed() (in MyApp.debug.dylib) (/<compiler-generated>:0)"
        report = _report()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", FakeTools(lines=lines)))
        [entry] = report.symbols
        assert (entry.frames_resolved, entry.frames_total) == (1, 2)
        assert "1 of 2 frames have no source line" in entry.note
        assert report.frames[2].file == "" and report.frames[2].line is None


class TestWhatIsLeftAlone:
    def test_system_frames_and_frames_with_lines(self, tmp_path):
        """A simulator's report has lines already; system frames are named."""
        report = _report()
        report.frames[1].file, report.frames[1].line = "Menu.swift", 170
        report.frames[2].file, report.frames[2].line = "Cell.swift", 37
        tools = FakeTools()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", tools))
        assert tools.calls == [] and report.symbols == []

    def test_an_image_outside_an_app_bundle(self, tmp_path):
        tools = FakeTools()
        report = _report(app_image_path="/usr/lib/libMyApp.dylib")
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", tools))
        assert tools.calls == []

    def test_an_android_frame_has_no_load_address(self, tmp_path):
        report = _report()
        for i in report.images:
            i.base = None
        tools = FakeTools()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", tools))
        assert tools.calls == []


class TestOnce:
    def test_a_report_is_symbolicated_once(self, tmp_path):
        dsym = _dsym(tmp_path / "d", U_APP)
        _record(tmp_path / "records", dsym)
        tools = FakeTools(lines=GOOD)
        finder = symbolicate.SymbolFinder(tmp_path / "records", tools)
        report = _report()
        _run([report], finder)
        _run([report], finder)
        assert len(tools.atos) == 1 and len(report.symbols) == 1

    def test_a_uuid_found_once_is_not_looked_up_again(self, tmp_path):
        dsym = _dsym(tmp_path / "DerivedData", U_APP)
        tools = FakeTools(lines=GOOD, mdfind=f"{dsym}\n")
        finder = symbolicate.SymbolFinder(tmp_path / "records", tools)
        first, second = _report(), _report()
        second.crash_id = "c2"
        _run([first], finder)
        _run([second], finder)
        assert [c[0] for c in tools.calls].count("mdfind") == 1

    def test_a_miss_is_looked_up_again(self, tmp_path):
        """A later build may make it."""
        tools = FakeTools(mdfind="")
        finder = symbolicate.SymbolFinder(tmp_path / "records", tools)
        first, second = _report(), _report()
        _run([first], finder)
        _run([second], finder)
        assert [c[0] for c in tools.calls].count("mdfind") == 2


class TestParse:
    @pytest.mark.parametrize("line, want", [
        ("closure #4 in Menu.tap() (in MyApp.debug.dylib) (Menu.swift:170)",
         ("closure #4 in Menu.tap()", None, "Menu.swift", 170)),
        ("-[UIView layoutSubviews] (in UIKitCore) + 12",
         ("-[UIView layoutSubviews]", 12, "", None)),
        ("@objc Cell.pressed() (in MyApp.debug.dylib) (/<compiler-generated>:0)",
         ("@objc Cell.pressed()", None, "", None)),
        ("0x00000040 (in MyApp.debug.dylib)", None),
        ("", None),
    ])
    def test_the_forms_atos_prints(self, line, want):
        assert symbolicate._parse(line) == want


# ── the route ────────────────────────────────────────────────────────────────

HEADERS = {"Authorization": "Bearer test-key-12345"}


@pytest.fixture
def crash_app(tmp_path):
    from server.config import ServerConfig
    from server.main import create_app
    from server.sources.crash import CrashAdapter

    app = create_app(config=ServerConfig(api_key="test-key-12345"),
                     enable_oslog=False, enable_crash=False, enable_proxy=False)
    app.state.crash_adapter = CrashAdapter(watch_dir=tmp_path / "crashes", poll_interval=3600)
    return app


async def _latest(app, **params):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/crashes/latest", headers=HEADERS, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestTheRoute:
    async def test_the_compact_answer_carries_the_line_and_where_it_came_from(
            self, crash_app, tmp_path):
        dsym = _dsym(tmp_path / "d", U_APP)
        _record(tmp_path / "records", dsym)
        finder = symbolicate.SymbolFinder(tmp_path / "records", FakeTools(lines=GOOD))
        crash_app.state.symbol_finder = finder
        crash_app.state.crash_adapter.crash_reports.append(_report())
        [crash] = (await _latest(crash_app))["crashes"]
        assert (crash["app_frame"]["file"], crash["app_frame"]["line"]) == ("Menu.swift", 170)
        assert crash["symbols"][0]["source"] == "build_record"
        assert crash["frames"] == [] and "symbolicated" not in crash

    async def test_symbolicate_false_leaves_it(self, crash_app, tmp_path):
        tools = FakeTools(lines=GOOD)
        crash_app.state.symbol_finder = symbolicate.SymbolFinder(tmp_path / "records", tools)
        crash_app.state.crash_adapter.crash_reports.append(_report())
        [crash] = (await _latest(crash_app, symbolicate="false"))["crashes"]
        assert tools.calls == [] and crash["symbols"] == []
        assert crash["app_frame"]["file"] == ""
