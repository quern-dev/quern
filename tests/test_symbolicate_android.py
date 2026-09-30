"""Android build records and symbolication (#326, step 4).

No Gradle, NDK or SDK tool runs here: ELF files are bytes built below, a
Gradle output tree is laid out by hand, and llvm-symbolizer and retrace are a
fake runner. Checked once for real: the ELF reader gave the same BuildIds as
the NDK's llvm-readelf for 12 libraries of a real app's build, and
record_android_build on that app's minified release variant kept its 192 MB
mapping.txt (pg_map_id read from the header) and 20 native libraries.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from server.device import build_records, elf
from server.models import CrashFrame, CrashImage, CrashReport
from server.sources import symbolicate, symbolicate_android

BUILD_ID = "a7846ae06d6bf6cf0e9d7c1b2a3f4e5d6c7b8a90"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
PKG = "com.example.app"


def elf_bytes(build_id: str = BUILD_ID, *, machine: int = 183, wide: bool = True,
              big: bool = False) -> bytes:
    """A minimal ELF with one PT_NOTE holding a GNU build-id note."""
    e = ">" if big else "<"
    desc = bytes.fromhex(build_id)
    note = struct.pack(e + "III", 4, len(desc), 3) + b"GNU\0" + desc
    note += b"\0" * (-len(note) % 4)
    if wide:
        ehsize, phentsize = 64, 56
        phoff = ehsize
        note_off = phoff + phentsize
        head = b"\x7fELF" + bytes([2, 2 if big else 1, 1]) + b"\0" * 9
        head += struct.pack(e + "HHIQQQIHHHHHH", 3, machine, 1, 0, phoff, 0, 0,
                            ehsize, phentsize, 1, 0, 0, 0)
        ph = struct.pack(e + "IIQQQQQQ", 4, 4, note_off, 0, 0, len(note), len(note), 4)
    else:
        ehsize, phentsize = 52, 32
        phoff = ehsize
        note_off = phoff + phentsize
        head = b"\x7fELF" + bytes([1, 2 if big else 1, 1]) + b"\0" * 9
        head += struct.pack(e + "HHIIIIIHHHHHH", 3, machine, 1, 0, phoff, 0, 0,
                            ehsize, phentsize, 1, 0, 0, 0)
        ph = struct.pack(e + "IIIIIIII", 4, note_off, 0, 0, len(note), len(note), 4, 4)
    return head + ph + note


class TestElf:
    @pytest.mark.parametrize("kw, abi", [
        ({}, "arm64-v8a"), ({"machine": 62}, "x86_64"),
        ({"wide": False, "machine": 40}, "armeabi-v7a"), ({"big": True}, "arm64-v8a"),
    ])
    def test_build_id_and_abi(self, tmp_path, kw, abi):
        p = tmp_path / "lib.so"
        p.write_bytes(elf_bytes(**kw))
        found = elf.read(p)
        assert (found.build_id, found.abi) == (BUILD_ID, abi)

    def test_no_note(self, tmp_path):
        data = bytearray(elf_bytes())
        struct.pack_into("<I", data, 64, 1)          # PT_LOAD, not PT_NOTE
        p = tmp_path / "lib.so"
        p.write_bytes(bytes(data))
        assert elf.read(p).build_id == ""

    @pytest.mark.parametrize("data", [
        b"", b"not an elf", elf_bytes()[:70], b"\x7fELF" + b"\0" * 60,
    ])
    def test_malformed_is_none_or_empty(self, tmp_path, data):
        p = tmp_path / "lib.so"
        p.write_bytes(data)
        found = elf.read(p)
        assert found is None or found.build_id == ""


# ── record_android_build ─────────────────────────────────────────────────────


def _gradle_module(tmp_path: Path, variant="stagingRelease", *, mapping=True, code=99999,
                   built=NOW - timedelta(minutes=5)) -> Path:
    module = tmp_path / "project" / "app"
    apk_dir = module / "build" / "outputs" / "apk" / "staging" / "release"
    apk_dir.mkdir(parents=True)
    apk = apk_dir / "app-staging-release.apk"
    apk.write_bytes(b"PK")
    os.utime(apk, (built.timestamp(), built.timestamp()))
    (apk_dir / "output-metadata.json").write_text(json.dumps({
        "applicationId": PKG, "variantName": variant,
        "elements": [{"versionCode": code, "versionName": "1.2.3",
                      "outputFile": "app-staging-release.apk"}]}))
    if mapping:
        m = module / "build" / "outputs" / "mapping" / variant
        m.mkdir(parents=True)
        (m / "mapping.txt").write_text("# compiler: R8\n# pg_map_id: e1ad14241ecb07832d\n"
                                       "com.example.app.Feed -> a.b:\n")
    libs = (module / "build" / "intermediates" / "merged_native_libs" / variant
            / "merge" / "out" / "lib")
    (libs / "arm64-v8a").mkdir(parents=True)
    (libs / "arm64-v8a" / "libapp.so").write_bytes(elf_bytes())
    (libs / "arm64-v8a" / "libnoid.so").write_bytes(elf_bytes()[:64])
    return module


def _record_android(module, root, variant="stagingRelease", now=NOW):
    return asyncio.run(build_records.record_android_build(module, variant, root=root, now=now))


class TestRecordAndroidBuild:
    def test_what_is_kept(self, tmp_path):
        record = _record_android(_gradle_module(tmp_path), tmp_path / "records")
        assert (record.platform, record.bundle_id, record.version, record.build_number) == (
            "android", PKG, "1.2.3", "99999")
        assert record.mapping_id == "e1ad14241ecb07832d"
        assert Path(record.mapping).read_text().startswith("# compiler: R8")
        [lib] = [b for b in record.binaries if b.dwarf]
        assert lib.path == "lib/arm64-v8a/libapp.so" and lib.uuids == {"arm64-v8a": BUILD_ID}
        assert Path(lib.dwarf).read_bytes() == elf_bytes()
        assert "kept its R8 mapping (e1ad14241ecb)" in build_records.summary_line(record)

    def test_a_fresh_build_carries_no_age_note(self, tmp_path):
        record = _record_android(_gradle_module(tmp_path), tmp_path / "records")
        assert record.notes == []

    def test_outputs_older_than_an_hour_say_so(self, tmp_path):
        # Recording what build/ held from weeks ago retraced a real crash to a
        # line seven lines from where today's source throws.
        module = _gradle_module(tmp_path, built=NOW - timedelta(days=82))
        record = _record_android(module, tmp_path / "records")
        [note] = record.notes
        assert "built 2026-07-09 12:00 UTC, 82 days before" in note
        assert "build again" in build_records.summary_line(record)

    def test_the_hour_is_the_line(self, tmp_path):
        old = _record_android(_gradle_module(tmp_path / "a", built=NOW - timedelta(minutes=61)),
                              tmp_path / "records")
        fresh = _record_android(_gradle_module(tmp_path / "b", built=NOW - timedelta(minutes=59)),
                                tmp_path / "records")
        assert "1 hour before" in old.notes[0] and fresh.notes == []

    def test_a_missing_apk_is_said(self, tmp_path):
        module = _gradle_module(tmp_path)
        next(module.rglob("*.apk")).unlink()
        record = _record_android(module, tmp_path / "records")
        assert record.notes == ["the APK its output-metadata.json names, "
                                "app-staging-release.apk, is not there"]

    def test_a_truncated_library_is_skipped(self, tmp_path):
        record = _record_android(_gradle_module(tmp_path), tmp_path / "records")
        assert not [b for b in record.binaries if b.path.endswith("libnoid.so")]

    def test_a_library_without_a_build_id_is_said(self, tmp_path):
        module = _gradle_module(tmp_path)
        data = bytearray(elf_bytes())
        struct.pack_into("<I", data, 64, 1)          # its note is not PT_NOTE: no BuildId
        lib = next(module.rglob("libnoid.so"))
        lib.write_bytes(bytes(data))
        record = _record_android(module, tmp_path / "records")
        [noid] = [b for b in record.binaries if b.path.endswith("libnoid.so")]
        assert "no BuildId" in noid.dsym_error and noid.dwarf == ""

    def test_an_unminified_variant_has_no_mapping(self, tmp_path):
        record = _record_android(_gradle_module(tmp_path, mapping=False), tmp_path / "records")
        assert record.mapping == "" and "no R8 mapping" in build_records.summary_line(record)

    def test_a_variant_not_built_names_those_that_were(self, tmp_path):
        with pytest.raises(build_records.AndroidBuildNotFound,
                           match="built variants: stagingRelease"):
            _record_android(_gradle_module(tmp_path), tmp_path / "records", variant="prodRelease")

    def test_no_outputs_at_all_says_where_it_looked(self, tmp_path):
        (tmp_path / "project").mkdir()
        with pytest.raises(build_records.AndroidBuildNotFound, match="app module's directory"):
            _record_android(tmp_path / "project", tmp_path / "records")

    def test_retention_expires_android_symbols_too(self, tmp_path):
        root = tmp_path / "records"
        module = _gradle_module(tmp_path)
        records = [_record_android(module, root, now=NOW.replace(minute=m)) for m in range(11)]
        build_records.prune(root, now=NOW.replace(hour=13))
        oldest = next(r for r in build_records.load_all(root) if r.build_id == records[0].build_id)
        assert oldest.dsyms_expired and oldest.mapping == ""
        assert not (root / oldest.build_id / "symbols").exists()


# ── symbolication ────────────────────────────────────────────────────────────


def _native_report(offset=0x16c8, image_path="/data/app/~~x/com.example.app/lib/arm64/libapp.so"):
    frames = [
        CrashFrame(image="libc.so", offset=0x9e498, symbol="abort", build_id="cd79"),
        CrashFrame(image="libapp.so", offset=offset, build_id=BUILD_ID, app=True),
    ]
    return CrashReport(
        crash_id="a1", timestamp=NOW, process=PKG, kind="native_crash", frames=frames,
        app_frame=frames[1], top_frames=["#00 pc 000000000009e498  /apex/libc.so (abort)"],
        images=[CrashImage(name="libapp.so", uuid=BUILD_ID, path=image_path)],
        file_path="dropbox:data_app_native_crash@2026-09-29 12:00:00", bundle_id=PKG,
        app_version="1.2.3", build_version="99999")


def _java_report(file="SourceFile"):
    frames = [
        CrashFrame(image="", symbol="a.b.c", file=file, line=3, app=True),
        CrashFrame(image="", symbol="android.os.Handler.dispatchMessage", file="Handler.java",
                   line=106),
    ]
    return CrashReport(
        crash_id="j1", timestamp=NOW, process=PKG, kind="crash", frames=frames,
        app_frame=frames[0], top_frames=["a.b.c(SourceFile:3)"],
        file_path="dropbox:data_app_crash@2026-09-29 12:00:00", bundle_id=PKG,
        app_version="1.2.3", build_version="99999")


class FakeAndroidTools:
    def __init__(self, symbolizer=None, retrace=None, code=0):
        self.calls = []
        self.symbolizer, self.retrace, self.code = symbolizer, retrace, code

    async def __call__(self, argv):
        self.calls.append(argv)
        if Path(argv[0]).name == "llvm-symbolizer":
            return self.code, self.symbolizer or "", ""
        text = Path(argv[2]).read_text()
        self.sent = text
        out = []
        for line in text.splitlines():
            out.append(self.retrace.get(line.strip(), line) if self.retrace else line)
        return self.code, "\n".join(out) + "\n", ""


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(symbolicate_android, "find_llvm_symbolizer", lambda: "/ndk/llvm-symbolizer")
    monkeypatch.setattr(symbolicate_android, "find_retrace", lambda: "/sdk/retrace")


def _run(reports, finder):
    asyncio.run(symbolicate.symbolicate_many(reports, finder))


#: llvm-symbolizer's plain output for one address.
SYMBOLIZED = "Crasher::boom()\n/src/app/cpp/crasher.cpp:42:7\n\n"


class TestNative:
    def test_a_recorded_library_gets_function_file_and_line(self, tmp_path, tools):
        root = tmp_path / "records"
        _record_android(_gradle_module(tmp_path), root)
        fake = FakeAndroidTools(symbolizer=SYMBOLIZED)
        report = _native_report()
        _run([report], symbolicate.SymbolFinder(root, fake))
        f = report.frames[1]
        assert (f.symbol, f.file, f.line) == ("Crasher::boom()", "crasher.cpp", 42)
        [entry] = report.symbols
        assert entry.source == "build_record" and entry.frames_resolved == 1
        assert fake.calls[0][:2] == ["/ndk/llvm-symbolizer", f"--obj={entry.dwarf}"]
        assert fake.calls[0][2] == "0x16c8"             # the pc as the tombstone gave it
        assert report.symbolicated

    def test_system_libraries_are_left_alone(self, tmp_path, tools):
        report = _native_report()
        report.frames[1].app = False
        report.app_frame = None
        fake = FakeAndroidTools()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", fake))
        assert fake.calls == [] and report.top_frames[0].startswith("#00 pc")

    def test_no_record_is_said_and_retried(self, tmp_path, tools):
        report = _native_report()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", FakeAndroidTools()))
        assert "no build record has libapp.so" in report.symbols[0].note
        assert not report.symbolicated

    def test_no_ndk_is_said_and_retried(self, tmp_path, monkeypatch):
        root = tmp_path / "records"
        _record_android(_gradle_module(tmp_path), root)
        monkeypatch.setattr(symbolicate_android, "find_llvm_symbolizer", lambda: None)
        report = _native_report()
        _run([report], symbolicate.SymbolFinder(root, FakeAndroidTools()))
        assert "install the Android NDK" in report.symbols[0].note and not report.symbolicated

    def test_a_name_without_debug_info(self, tmp_path, tools):
        root = tmp_path / "records"
        _record_android(_gradle_module(tmp_path), root)
        nameonly = "JNI_OnLoad\n??:0:0\n\n"             # real, from a library with no DWARF
        report = _native_report()
        _run([report], symbolicate.SymbolFinder(root, FakeAndroidTools(symbolizer=nameonly)))
        assert report.frames[1].symbol == "JNI_OnLoad" and report.frames[1].file == ""
        assert "no debug information" in report.symbols[0].note and report.symbolicated


RETRACED = {"at a.b.c(SourceFile:3)": "\tat com.example.app.Feed.parse(Feed.kt:12)"}


class TestJava:
    def test_a_minified_frame_is_retraced(self, tmp_path, tools):
        root = tmp_path / "records"
        record = _record_android(_gradle_module(tmp_path), root)
        fake = FakeAndroidTools(retrace=RETRACED)
        report = _java_report()
        _run([report], symbolicate.SymbolFinder(root, fake))
        f = report.frames[0]
        assert (f.symbol, f.file, f.line) == ("com.example.app.Feed.parse", "Feed.kt", 12)
        assert report.app_frame.symbol == "com.example.app.Feed.parse"
        [entry] = report.symbols
        assert (entry.source, entry.build_id, entry.uuid) == (
            "build_record", record.build_id, "e1ad14241ecb07832d")
        assert fake.calls[0][:2] == ["/sdk/retrace", record.mapping]
        assert "com.example.app.Feed.parse" in report.top_frames[0]
        assert "Feed.kt:12" in report.top_frames[0]

    def test_only_frames_with_r8_marks_are_sent(self, tmp_path, tools):
        root = tmp_path / "records"
        _record_android(_gradle_module(tmp_path), root)
        fake = FakeAndroidTools(retrace=RETRACED)
        report = _java_report()
        _run([report], symbolicate.SymbolFinder(root, fake))
        assert "a.b.c(SourceFile:3)" in fake.sent
        assert "Handler" not in fake.sent                # its real file name: not R8's
        assert report.frames[1].symbol == "android.os.Handler.dispatchMessage"

    def test_a_debug_builds_frames_need_nothing(self, tmp_path, tools):
        report = _java_report(file="Feed.kt")
        fake = FakeAndroidTools()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", fake))
        assert fake.calls == [] and report.symbols == [] and report.symbolicated

    def test_the_map_id_stamp_picks_the_mapping_exactly(self, tmp_path, tools):
        root = tmp_path / "records"
        _record_android(_gradle_module(tmp_path), root, now=NOW.replace(minute=1))
        other = _gradle_module(tmp_path / "other")
        mapping = other / "build/outputs/mapping/stagingRelease/mapping.txt"
        mapping.write_text("# compiler: R8\n# pg_map_id: 00ff00ff\n")
        newer = _record_android(other, root, now=NOW.replace(minute=2))
        report = _java_report(file="r8-map-id-e1ad14241ecb07832d")
        fake = FakeAndroidTools(retrace={"at a.b.c(r8-map-id-e1ad14241ecb07832d:3)":
                                         "\tat com.example.app.Feed.parse(Feed.kt:12)"})
        _run([report], symbolicate.SymbolFinder(root, fake))
        assert report.symbols[0].uuid == "e1ad14241ecb07832d"
        assert report.symbols[0].build_id != newer.build_id

    def test_a_stamp_with_no_record_is_said(self, tmp_path, tools):
        report = _java_report(file="r8-map-id-deadbeef")
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", FakeAndroidTools()))
        assert "no build record has the R8 mapping deadbeef" in report.symbols[0].note
        assert not report.symbolicated

    def test_builds_sharing_a_version_are_said(self, tmp_path, tools):
        root = tmp_path / "records"
        module = _gradle_module(tmp_path)
        _record_android(module, root, now=NOW.replace(minute=1))
        _record_android(module, root, now=NOW.replace(minute=2))
        report = _java_report()
        _run([report], symbolicate.SymbolFinder(root, FakeAndroidTools(retrace=RETRACED)))
        assert "2 recorded builds share them" in report.symbols[0].note

    def test_no_retrace_is_said_and_retried(self, tmp_path, monkeypatch):
        root = tmp_path / "records"
        _record_android(_gradle_module(tmp_path), root)
        monkeypatch.setattr(symbolicate_android, "find_retrace", lambda: None)
        report = _java_report()
        _run([report], symbolicate.SymbolFinder(root, FakeAndroidTools()))
        assert "command-line tools" in report.symbols[0].note and not report.symbolicated

    def test_ambiguous_and_inlined_frames_are_said(self, tmp_path, tools):
        root = tmp_path / "records"
        _record_android(_gradle_module(tmp_path), root)
        retraced = {"at a.b.c(SourceFile:3)":
                    "\tat com.example.app.Inner.f(Inner.kt:5)\n"
                    "\tat com.example.app.Feed.parse(Feed.kt:12)\n"
                    "\t<OR> at com.example.app.Other.g(Other.kt:9)"}
        report = _java_report()
        _run([report], symbolicate.SymbolFinder(root, FakeAndroidTools(retrace=retraced)))
        assert report.frames[0].symbol == "com.example.app.Inner.f"
        note = report.symbols[0].note
        assert "ambiguous" in note and "inlined" in note

    def test_output_that_does_not_match_the_frames_is_not_guessed(self, tmp_path, tools):
        root = tmp_path / "records"
        _record_android(_gradle_module(tmp_path), root)

        async def garbled(argv):
            return 0, "something else entirely\n", ""

        report = _java_report()
        _run([report], symbolicate.SymbolFinder(root, garbled))
        assert report.frames[0].symbol == "a.b.c"
        assert "could not be matched" in report.symbols[0].note


class TestTheTools:
    def test_the_newest_ndk_is_used(self, tmp_path, monkeypatch):
        """The version was read from the wrong path segment: NDK 23 over 27."""
        for version in ("23.1.7779620", "27.1.12297006", "26.1.10909125"):
            tool = (tmp_path / "ndk" / version / "toolchains" / "llvm" / "prebuilt"
                    / "darwin-x86_64" / "bin" / "llvm-symbolizer")
            tool.parent.mkdir(parents=True)
            tool.write_text("")
        monkeypatch.setenv("ANDROID_HOME", str(tmp_path))
        assert "/27.1.12297006/" in symbolicate_android.find_llvm_symbolizer()

    def test_an_unknown_address_and_inlining(self, tmp_path, tools):
        """`??` for what it does not know; inlined frames innermost first."""
        root = tmp_path / "records"
        _record_android(_gradle_module(tmp_path), root)
        report = _native_report()
        report.frames.append(CrashFrame(image="libapp.so", offset=0x2000, build_id=BUILD_ID,
                                        app=True))
        out = ("inner()\n/src/a.cpp:3:1\nouter()\n/src/a.cpp:9:2\n\n"
               "??\n??:0:0\n\n")
        _run([report], symbolicate.SymbolFinder(root, FakeAndroidTools(symbolizer=out)))
        assert (report.frames[1].symbol, report.frames[1].line) == ("inner()", 3)
        assert report.frames[2].symbol == "" and report.frames[2].file == ""

    def test_answers_that_do_not_match_the_addresses_are_not_guessed(self, tmp_path, tools):
        root = tmp_path / "records"
        _record_android(_gradle_module(tmp_path), root)
        report = _native_report()
        out = SYMBOLIZED + SYMBOLIZED                  # two answers for one address
        _run([report], symbolicate.SymbolFinder(root, FakeAndroidTools(symbolizer=out)))
        assert report.frames[1].symbol == ""
        assert "2 answers for 1 addresses" in report.symbols[0].note


class TestUnknownSource:
    def test_a_frame_with_no_file_but_a_line_is_retraced(self, tmp_path, tools):
        """A real release build's crash: its rules strip the source file."""
        root = tmp_path / "records"
        _record_android(_gradle_module(tmp_path), root)
        report = _java_report()
        top = report.frames[0]
        top.symbol, top.file, top.line = "l82.onClick", "", 539
        fake = FakeAndroidTools(retrace={"at l82.onClick(Unknown Source:539)":
                                         "\tat com.example.app.debug.DebugMenuFragment.onCreateView"
                                         "$lambda$0$13(DebugMenuFragment.kt:304)"})
        _run([report], symbolicate.SymbolFinder(root, fake))
        assert "at l82.onClick(Unknown Source:539)" in fake.sent
        f = report.frames[0]
        assert (f.file, f.line) == ("DebugMenuFragment.kt", 304)

    def test_no_file_and_no_line_is_not_sent(self, tmp_path, tools):
        report = _java_report()
        report.frames[0].file, report.frames[0].line = "", None
        fake = FakeAndroidTools()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", fake))
        assert fake.calls == []
