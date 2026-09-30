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
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import ServerConfig
from server.device import build_records, elf
from server.main import create_app
from server.models import CrashFrame, CrashImage, CrashReport
from server.sources import android_dropbox, crash_frames, symbolicate, symbolicate_android

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


def _with_notes(first: bytes) -> bytes:
    """elf_bytes() with another note in front of the build-id note."""
    data = elf_bytes()
    head, note = data[:64 + 56], data[64 + 56:]
    ph = bytearray(head)
    size = len(first) + len(note)
    struct.pack_into("<QQ", ph, 64 + 32, size, size)  # p_filesz, p_memsz
    return bytes(ph) + first + note


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

    @pytest.mark.parametrize("name, kind", [(b"Andro\0", 3), (b"GNU\0", 1)])
    def test_only_the_gnu_build_id_note_is_the_build_id(self, tmp_path, name, kind):
        """Another note can come first -- Android's own, a type-1 GNU ABI tag --
        and a name whose length is not a multiple of four is padded, which
        the reader must step over."""
        other = struct.pack("<III", len(name.rstrip(b"\0")) + 1, 4, kind) + name
        other += b"\0" * (-len(other) % 4) + b"\x1c\0\0\0"
        p = tmp_path / "lib.so"
        p.write_bytes(_with_notes(other))
        assert elf.read(p).build_id == BUILD_ID

    def test_a_note_past_its_segment_is_not_read(self, tmp_path):
        data = bytearray(elf_bytes())
        struct.pack_into("<I", data, 64 + 56, 1 << 20)   # descsz: far past the segment
        p = tmp_path / "lib.so"
        p.write_bytes(bytes(data))
        assert elf.read(p) is None

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

    def test_a_mapping_without_a_map_id_is_said(self, tmp_path):
        module = _gradle_module(tmp_path)
        next(module.rglob("mapping.txt")).write_text(
            "# compiler: R8\ncom.example.app.Feed -> a.b:\n# pg_map_id: 0badc0de\n")
        record = _record_android(module, tmp_path / "records")
        # Only the header is read: a 192 MB mapping is not scanned for it.
        assert record.mapping_id == "" and "no pg_map_id" in record.notes[0]

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
    def __init__(self, symbolizer=None, retrace=None, code=0, raises=None):
        self.calls = []
        self.symbolizer, self.retrace, self.code = symbolizer, retrace, code
        self.raises = raises

    async def __call__(self, argv):
        self.calls.append(argv)
        if self.raises:
            raise self.raises
        if Path(argv[0]).name == "llvm-symbolizer":
            return self.code, self.symbolizer or "", ""
        text = Path(argv[2]).read_text()
        self.sent = text
        out = []
        # As the real retrace does (measured): a frame it knows becomes its
        # lines, each carrying the frame's `~[…]` suffix; one it does not
        # passes through, suffix and all; exception lines pass through.
        for line in text.splitlines():
            frame, tag = (line.rsplit(" ~[", 1) + [""])[:2]
            known = (self.retrace or {}).get(frame.strip())
            if known is None or not tag:
                out.append(line)
            else:
                out += [f"{x} ~[{tag}" for x in known.splitlines()]
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

    def test_the_trace_is_sent_whole_with_its_exception(self, tmp_path, tools):
        """retrace rewrites an NPE's frames only with the exception line in
        front of them, and resolves an outline from the frame after it: sent
        frame by frame, an NPE named the inlined callee (measured)."""
        root = tmp_path / "records"
        _record_android(_gradle_module(tmp_path), root)
        fake = FakeAndroidTools(retrace=RETRACED)
        report = _java_report()
        report.frames[0].thrown = "java.lang.NullPointerException: boom"
        _run([report], symbolicate.SymbolFinder(root, fake))
        assert fake.sent == ("java.lang.NullPointerException: boom\n"
                             "\tat a.b.c(SourceFile:3) ~[QUERN-0]\n"
                             "\tat android.os.Handler.dispatchMessage(Handler.java:106)"
                             " ~[QUERN-1]\n")
        assert report.frames[1].symbol == "android.os.Handler.dispatchMessage"

    def test_a_debug_builds_frames_need_nothing(self, tmp_path, tools):
        report = _java_report(file="Feed.kt")
        fake = FakeAndroidTools()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", fake))
        # Not settled: its record may be made later, and asking again is a scan.
        assert fake.calls == [] and report.symbols == [] and not report.symbolicated

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
        assert "2 recorded builds share it" in report.symbols[0].note

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
        for version in ("23.1.7779620", "27.1.12297006", "26.1.10909125", "9.0.1"):
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


# ── the paths that must not settle, and the ones that must ────────────────────


def _recorded(tmp_path):
    root = tmp_path / "records"
    _record_android(_gradle_module(tmp_path), root)
    return root


class TestNativeFailures:
    @pytest.mark.parametrize("fake, said", [
        (FakeAndroidTools(raises=OSError("exec format error")), "could not run: OSError"),
        (FakeAndroidTools(raises=TimeoutError()), "could not run: TimeoutError"),
        (FakeAndroidTools(code=-9), "exited -9"),
    ])
    def test_a_symbolizer_that_did_not_answer_is_retried(self, tmp_path, tools, fake, said):
        report = _native_report()
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert said in report.symbols[0].note and not report.symbolicated

    def test_a_failed_run_is_retried_and_says_why(self, tmp_path, tools):
        report = _native_report()

        async def fails(argv):
            return 1, "error: cannot open file\n", ""

        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fails))
        assert report.symbols[0].note == "llvm-symbolizer exited 1: error: cannot open file"
        assert not report.symbolicated

    def test_the_records_copy_gone_is_said_and_retried(self, tmp_path, tools):
        root = _recorded(tmp_path)
        next(root.rglob("libapp.so")).unlink()
        report = _native_report()
        fake = FakeAndroidTools(symbolizer=SYMBOLIZED)
        _run([report], symbolicate.SymbolFinder(root, fake))
        assert "is gone, unreadable, or not the library" in report.symbols[0].note
        assert not report.symbolicated and fake.calls == []

    def test_a_copy_that_is_another_library_is_not_used(self, tmp_path, tools):
        """A record is not proof of what is on disk."""
        root = _recorded(tmp_path)
        next(root.rglob("libapp.so")).write_bytes(elf_bytes("00" * 20))
        report = _native_report()
        fake = FakeAndroidTools(symbolizer=SYMBOLIZED)
        _run([report], symbolicate.SymbolFinder(root, fake))
        assert "not the library with BuildId" in report.symbols[0].note and fake.calls == []

    def test_an_older_record_is_used_when_the_newest_copy_is_gone(self, tmp_path, tools):
        root = tmp_path / "records"
        module = _gradle_module(tmp_path)
        older = _record_android(module, root, now=NOW.replace(minute=1))
        newer = _record_android(module, root, now=NOW.replace(minute=2))
        next((root / newer.build_id).rglob("libapp.so")).unlink()
        report = _native_report()
        _run([report], symbolicate.SymbolFinder(root, FakeAndroidTools(symbolizer=SYMBOLIZED)))
        assert report.symbols[0].build_id == older.build_id and report.frames[1].line == 42

    def test_an_expired_record_is_not_used(self, tmp_path, tools):
        root = _recorded(tmp_path)
        [path] = root.glob("*/record.json")
        data = json.loads(path.read_text())
        data["dsyms_expired"] = True
        path.write_text(json.dumps(data))
        report = _native_report()
        _run([report], symbolicate.SymbolFinder(root, FakeAndroidTools(symbolizer=SYMBOLIZED)))
        assert "expired with its build record" in report.symbols[0].note
        assert not report.symbolicated

    def test_an_upper_case_build_id_matches(self, tmp_path, tools):
        report = _native_report()
        report.frames[1].build_id = BUILD_ID.upper()
        fake = FakeAndroidTools(symbolizer=SYMBOLIZED)
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert report.frames[1].line == 42


class TestNativeNames:
    def test_the_line_comes_with_its_function(self, tmp_path, tools):
        """The tombstone names the symbol-table function; with inlining the line
        is in another one, and a name and line from two functions is wrong."""
        report = _native_report()
        f = report.frames[1]
        f.symbol, f.symbol_offset = "_ZN7Crasher3runEv", 44
        out = "Crasher::boom()\n/src/a.cpp:3:1\nCrasher::run()\n/src/a.cpp:9:2\n\n"
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path),
                                                FakeAndroidTools(symbolizer=out)))
        assert (f.symbol, f.symbol_offset, f.line) == ("Crasher::boom()", None, 3)
        assert "1 frames were inlined" in report.symbols[0].note

    def test_a_name_alone_does_not_replace_the_tombstones(self, tmp_path, tools):
        report = _native_report()
        f = report.frames[1]
        f.symbol, f.symbol_offset = "JNI_OnLoad", 44
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path),
                                                FakeAndroidTools(symbolizer="other\n??:0:0\n\n")))
        assert (f.symbol, f.symbol_offset) == ("JNI_OnLoad", 44)
        assert "got a function name" not in report.symbols[0].note

    def test_a_frame_with_a_file_is_not_sent(self, tmp_path, tools):
        report = _native_report()
        report.frames[1].file, report.frames[1].line = "crasher.cpp", 42
        fake = FakeAndroidTools(symbolizer=SYMBOLIZED)
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert fake.calls == []

    def test_the_app_frame_past_the_cap_is_resolved(self, tmp_path, tools):
        report = _native_report()
        beyond = report.frames.pop()                  # kept only as app_frame
        fake = FakeAndroidTools(symbolizer=SYMBOLIZED)
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert beyond.line == 42 and report.app_frame is beyond


class TestJavaFailures:
    @pytest.mark.parametrize("fake, said", [
        (FakeAndroidTools(raises=OSError("no java")), "could not run: OSError"),
        (FakeAndroidTools(raises=TimeoutError()), "could not run: TimeoutError"),
        (FakeAndroidTools(code=-15), "exited -15"),
    ])
    def test_a_retrace_that_did_not_answer_is_retried(self, tmp_path, tools, fake, said):
        report = _java_report()
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert said in report.symbols[0].note and not report.symbolicated
        assert report.top_frames == ["a.b.c(SourceFile:3)"]    # the trace's own text

    def test_the_records_mapping_gone_is_said_and_retried(self, tmp_path, tools):
        root = _recorded(tmp_path)
        next(root.rglob("mapping.txt")).unlink()
        report = _java_report()
        fake = FakeAndroidTools(retrace=RETRACED)
        _run([report], symbolicate.SymbolFinder(root, fake))
        assert "mapping.txt is gone" in report.symbols[0].note
        assert not report.symbolicated and fake.calls == []

    @pytest.mark.parametrize("change", [
        {"bundle_id": "com.example.other"}, {"build_version": "100000"},
        {"app_version": "1.2.4"},
    ])
    def test_another_version_is_not_matched(self, tmp_path, tools, change):
        report = _java_report().model_copy(update=change)
        fake = FakeAndroidTools(retrace=RETRACED)
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert "no build record has an R8 mapping" in report.symbols[0].note
        assert fake.calls == []

    def test_a_stamp_with_no_record_does_not_fall_back_to_the_version(self, tmp_path, tools):
        """The stamp names the build exactly; the version would name another."""
        report = _java_report(file="r8-map-id-deadbeef")
        fake = FakeAndroidTools(retrace=RETRACED)
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert fake.calls == [] and "deadbeef" in report.symbols[0].note

    def test_of_builds_sharing_a_version_the_newest_is_used(self, tmp_path, tools):
        root = tmp_path / "records"
        module = _gradle_module(tmp_path)
        _record_android(module, root, now=NOW.replace(minute=2))
        older = _record_android(module, root, now=NOW.replace(minute=1))
        newest = _record_android(module, root, now=NOW.replace(minute=3))
        report = _java_report()
        _run([report], symbolicate.SymbolFinder(root, FakeAndroidTools(retrace=RETRACED)))
        assert report.symbols[0].build_id == newest.build_id != older.build_id


class TestRetraceOutput:
    """Shapes taken from the real retrace (R8 9.4.14) on a hand-made mapping."""

    def _retrace(self, tmp_path, out_for_frame, file="SourceFile", line=3):
        report = _java_report(file=file)
        report.frames[0].line = line
        key = f"at a.b.c({file or 'Unknown Source'}" + (f":{line})" if line else ")")
        _run([report], symbolicate.SymbolFinder(
            _recorded(tmp_path), FakeAndroidTools(retrace={key: out_for_frame})))
        return report

    def test_an_unmapped_frame_passes_through_unresolved(self, tmp_path, tools):
        report = self._retrace(tmp_path, "\tat a.b.c(SourceFile:3)")
        f = report.frames[0]
        assert (f.symbol, f.file, f.line) == ("a.b.c", "", 3)
        # Counted as a frame to retrace (it carries R8's mark); Handler is not.
        assert (report.symbols[0].frames_resolved, report.symbols[0].frames_total) == (0, 1)
        assert "1 of 1 renamed frames have no source line" in report.symbols[0].note

    def test_a_name_without_a_line_is_not_resolved(self, tmp_path, tools):
        report = self._retrace(tmp_path, "\tat com.example.app.Feed.load(Feed.java)\n"
                                         "\t<OR> at com.example.app.Feed.parse(Feed.java)",
                               line=None)
        f = report.frames[0]
        assert (f.symbol, f.file, f.line) == ("com.example.app.Feed.load", "Feed.java", None)
        assert (report.symbols[0].frames_resolved, report.symbols[0].frames_total) == (0, 1)

    def test_ambiguous_alone_is_not_inlined(self, tmp_path, tools):
        report = self._retrace(tmp_path, "\tat com.example.app.Feed.load(Feed.kt:7)\n"
                                         "\t<OR> at com.example.app.Feed.parse(Feed.kt:9)")
        note = report.symbols[0].note
        assert "ambiguous" in note and "inlined" not in note

    def test_the_app_frame_moves_to_the_real_app_frame(self, tmp_path, tools):
        """An obfuscated name tells nothing of whose code it is: `l82` looked
        like the app's, and after retracing it is the framework's."""
        report = _java_report()
        report.frames.insert(0, CrashFrame(image="", symbol="l82.a", file="SourceFile", line=1,
                                           app=True))
        report.app_frame = report.frames[0]
        fake = FakeAndroidTools(retrace={
            "at l82.a(SourceFile:1)": "\tat androidx.fragment.app.Fragment.f(Fragment.java:5)",
            **RETRACED})
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert report.frames[0].app is False
        assert report.app_frame.symbol == "com.example.app.Feed.parse"


class TestSettled:
    def test_a_second_read_keeps_what_settled_and_asks_nothing(self, tmp_path, tools):
        root = _recorded(tmp_path)
        fake = FakeAndroidTools(retrace=RETRACED, symbolizer=SYMBOLIZED)
        report = _java_report()
        report.frames += _native_report().frames[1:]
        finder = symbolicate.SymbolFinder(root, fake)
        _run([report], finder)
        first = [(e.image, e.frames_resolved) for e in report.symbols]
        report.symbolicated = False                     # as a fresh read of the same report
        _run([report], finder)
        assert len(fake.calls) == 2
        assert [(e.image, e.frames_resolved) for e in report.symbols] == first
        assert sorted(first) == [("java (R8 mapping)", 1), ("libapp.so", 1)]

    def test_an_unsettled_image_is_asked_again(self, tmp_path, monkeypatch):
        root = _recorded(tmp_path)
        monkeypatch.setattr(symbolicate_android, "find_llvm_symbolizer",
                            lambda: "/ndk/llvm-symbolizer")
        monkeypatch.setattr(symbolicate_android, "find_retrace", lambda: None)
        fake = FakeAndroidTools(retrace=RETRACED, symbolizer=SYMBOLIZED)
        report = _java_report()
        report.frames += _native_report().frames[1:]
        finder = symbolicate.SymbolFinder(root, fake)
        _run([report], finder)
        monkeypatch.setattr(symbolicate_android, "find_retrace", lambda: "/sdk/retrace")
        _run([report], finder)
        assert [Path(c[0]).name for c in fake.calls] == ["llvm-symbolizer", "retrace"]
        assert report.symbolicated and report.frames[0].file == "Feed.kt"


class TestTheSubprocess:
    @pytest.mark.parametrize("tool, timeout", [
        ("/sdk/cmdline-tools/latest/bin/retrace", symbolicate_android.RETRACE_TIMEOUT),
        ("/ndk/bin/llvm-symbolizer", symbolicate.TOOL_TIMEOUT),
    ])
    def test_retrace_gets_its_longer_timeout(self, monkeypatch, tool, timeout):
        seen = {}

        class Proc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def spawn(*argv, **kw):
            return Proc()

        async def wait_for(aw, timeout):
            seen["timeout"] = timeout
            return await aw

        monkeypatch.setattr(symbolicate.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(symbolicate.asyncio, "wait_for", wait_for)
        asyncio.run(symbolicate._run([tool, "x"]))
        assert seen["timeout"] == timeout


# ── review round 1 ───────────────────────────────────────────────────────────


def _apk_with_map_id(module: Path, map_id: str | None) -> None:
    apk = next(module.rglob("*.apk"))
    stamp = apk.stat().st_mtime
    with zipfile.ZipFile(apk, "w") as z:
        marker = (f'~~R8{{"backend":"dex","compilation-mode":"release","pg-map-id":"{map_id}"}}'
                  if map_id else "no marker")
        z.writestr("classes.dex", b"dex\n035\0" + marker.encode())
    os.utime(apk, (stamp, stamp))


class TestTheApksOwnMapId:
    def test_a_mapping_from_another_build_is_not_kept(self, tmp_path):
        module = _gradle_module(tmp_path)
        _apk_with_map_id(module, "0123456789ab")
        record = _record_android(module, tmp_path / "records")
        assert record.mapping == "" and record.mapping_id == ""
        assert "is not the one the APK was built with (0123456789ab)" in record.notes[0]

    def test_the_mapping_the_apk_was_built_with_is_kept(self, tmp_path):
        module = _gradle_module(tmp_path)
        _apk_with_map_id(module, "e1ad14241ecb07832d")
        record = _record_android(module, tmp_path / "records")
        assert record.mapping_id == "e1ad14241ecb07832d" and record.notes == []

    def test_a_minified_apk_with_no_mapping_is_said(self, tmp_path):
        module = _gradle_module(tmp_path, mapping=False)
        _apk_with_map_id(module, "0123456789ab")
        record = _record_android(module, tmp_path / "records")
        assert "minified by R8 (0123456789ab) but there is no mapping.txt" in record.notes[0]

    def test_an_apk_with_no_marker_takes_the_mapping_on_trust(self, tmp_path):
        module = _gradle_module(tmp_path)
        _apk_with_map_id(module, None)
        assert _record_android(module, tmp_path / "records").mapping_id == "e1ad14241ecb07832d"


class TestRecordEdges:
    @pytest.mark.parametrize("elements", [{"versionCode": 1}, ["not an element"], []])
    def test_metadata_not_in_agps_form_is_refused(self, tmp_path, elements):
        module = _gradle_module(tmp_path)
        meta = next(module.rglob("output-metadata.json"))
        meta.write_text(json.dumps({"applicationId": PKG, "variantName": "stagingRelease",
                                    "elements": elements}))
        with pytest.raises(build_records.AndroidBuildNotFound, match="form AGP writes"):
            _record_android(module, tmp_path / "records")

    def test_abi_splits_keep_every_version_code(self, tmp_path):
        module = _gradle_module(tmp_path)
        meta = next(module.rglob("output-metadata.json"))
        data = json.loads(meta.read_text())
        data["elements"] = [dict(data["elements"][0], versionCode=1001),
                            dict(data["elements"][0], versionCode=2001)]
        meta.write_text(json.dumps(data))
        record = _record_android(module, tmp_path / "records")
        assert record.version_codes == ["1001", "2001"]
        report = _java_report().model_copy(update={"build_version": "2001"})
        record, _, _ = symbolicate_android._mapping_for(report, report.frames, [record])
        assert record is not None

    def test_two_libraries_of_one_name_are_both_kept(self, tmp_path):
        """A stale output directory beside the current one: the copies
        overwrote each other and the record named one while holding the other."""
        module = _gradle_module(tmp_path)
        stale = (module / "build/intermediates/merged_native_libs/stagingRelease/out/lib"
                 / "arm64-v8a")
        stale.mkdir(parents=True)
        (stale / "libapp.so").write_bytes(elf_bytes("11" * 20))
        record = _record_android(module, tmp_path / "records")
        kept = {b.uuids["arm64-v8a"]: b.dwarf for b in record.binaries if b.dwarf}
        assert set(kept) == {BUILD_ID, "11" * 20}
        for build_id, path in kept.items():
            assert elf.read(Path(path)).build_id == build_id

    def test_a_failed_write_leaves_no_mapping_behind(self, tmp_path, monkeypatch):
        real = build_records.shutil.copy2

        def copy2(src, dst, *a, **kw):
            if str(src).endswith(".so"):
                raise OSError(28, "No space left on device")
            return real(src, dst, *a, **kw)

        monkeypatch.setattr(build_records.shutil, "copy2", copy2)
        record = _record_android(_gradle_module(tmp_path), tmp_path / "records")
        assert "No space left" in record.error
        assert (record.mapping, record.mapping_id) == ("", "")
        assert not any(b.dwarf for b in record.binaries)
        assert not list((tmp_path / "records").iterdir())


class TestAppFrameAfterRetracing:
    def test_the_root_cause_rule_holds(self, tmp_path, tools):
        """The first app frame of the innermost cause, as the trace was read:
        not the wrapper that rethrew it."""
        body = ("java.lang.RuntimeException: wrapped\n"
                "\tat a.w(SourceFile:1)\n"
                "Caused by: java.lang.IllegalStateException: boom\n"
                "\tat a.x(SourceFile:2)\n")
        frames, app_frame, _ = android_dropbox._java_trace(body, PKG)
        assert [f.thrown for f in frames] == ["java.lang.RuntimeException: wrapped",
                                              "Caused by: java.lang.IllegalStateException: boom"]
        report = _java_report().model_copy(update={"frames": frames, "app_frame": app_frame})
        fake = FakeAndroidTools(retrace={
            "at a.w(SourceFile:1)": "\tat com.example.app.Wrapper.run(Wrapper.kt:5)",
            "at a.x(SourceFile:2)": "\tat com.example.app.Cause.boom(Cause.kt:9)"})
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert report.app_frame.symbol == "com.example.app.Cause.boom"
        assert "Caused by: java.lang.IllegalStateException: boom\n\tat a.x" in fake.sent

    def test_a_frame_r8_made_up_is_folded_away(self, tmp_path, tools):
        """retrace writes nothing for an outline; its caller takes the line."""
        report = _java_report()
        report.frames.insert(0, CrashFrame(symbol="s75.l", line=3))
        fake = FakeAndroidTools(retrace={"at s75.l(Unknown Source:3)": "", **RETRACED})
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert [f.symbol for f in report.frames][:1] == ["com.example.app.Feed.parse"]
        assert "retrace wrote nothing for 1 frames" in report.symbols[0].note
        assert "no source line" not in report.symbols[0].note


class TestWhatIsSent:
    def test_a_kept_source_file_is_retraced_when_a_mapping_matches(self, tmp_path, tools):
        """`-keepattributes SourceFile` without renaming: R8's names, real
        file names, no mark at all."""
        report = _java_report(file="Feed.kt")
        fake = FakeAndroidTools(retrace={"at a.b.c(Feed.kt:3)":
                                         "\tat com.example.app.Feed.parse(Feed.kt:12)"})
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert report.frames[0].symbol == "com.example.app.Feed.parse"

    def test_a_debug_builds_synthetic_frames_ask_for_nothing(self, tmp_path, tools):
        """D8's lambdas print `(Unknown Source:2)` in a debug build too."""
        report = _java_report(file="Feed.kt")
        report.frames[0].symbol = "com.example.app.Feed$$ExternalSyntheticLambda0.run"
        report.frames[0].file, report.frames[0].line = "", 2
        fake = FakeAndroidTools()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", fake))
        assert fake.calls == [] and report.symbols == []

    def test_a_line_of_zero_is_sent(self, tmp_path, tools):
        report = _java_report()
        report.frames[0].line = 0
        fake = FakeAndroidTools(retrace=RETRACED)
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert "at a.b.c(SourceFile:0) ~[QUERN-0]" in fake.sent

    def test_a_version_only_match_always_says_so(self, tmp_path, tools):
        report = _java_report()
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path),
                                                FakeAndroidTools(retrace=RETRACED)))
        assert "matched by package and version only" in report.symbols[0].note

    def test_an_exact_stamp_says_nothing_of_versions(self, tmp_path, tools):
        report = _java_report(file="r8-map-id-e1ad14241ecb07832d")
        fake = FakeAndroidTools(retrace={"at a.b.c(r8-map-id-e1ad14241ecb07832d:3)":
                                         "\tat com.example.app.Feed.parse(Feed.kt:12)"})
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert report.symbols[0].note == ""

    def test_retrace_failing_says_why_from_stdout(self, tmp_path, tools):
        """The SDK script prints why on stdout and exits 1; that settled for
        good with an empty reason."""
        async def no_java(argv):
            return 1, "ERROR: JAVA_HOME is not set and no 'java' command could be found\n", ""

        report = _java_report()
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), no_java))
        assert "retrace exited 1: ERROR: JAVA_HOME is not set" in report.symbols[0].note
        assert not report.symbolicated


class TestCouldNotAsk:
    def test_an_unreadable_record_is_not_no_record(self, tmp_path, tools):
        root = tmp_path / "records"
        (root / "20260101-000000-android-bad").mkdir(parents=True)
        (root / "20260101-000000-android-bad" / "record.json").write_text("{not json")
        report = _native_report()
        _run([report], symbolicate.SymbolFinder(root, FakeAndroidTools()))
        assert "1 build record could not be read" in report.symbols[0].note

    def test_an_unlistable_records_directory_is_said(self, tmp_path, tools, monkeypatch):
        monkeypatch.setattr(build_records, "load_with_unreadable", lambda root: ([], 0, False))
        report = _java_report(file="r8-map-id-deadbeef")
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", FakeAndroidTools()))
        assert "directory could not be read" in report.symbols[0].note


class TestFindRetrace:
    def test_the_sdks_retrace_before_one_on_path(self, tmp_path, monkeypatch):
        """Homebrew's ProGuard installs a `retrace` that is another program."""
        sdk = tmp_path / "sdk" / "cmdline-tools" / "latest" / "bin" / "retrace"
        sdk.parent.mkdir(parents=True)
        sdk.write_text("")
        monkeypatch.setenv("ANDROID_HOME", str(tmp_path / "sdk"))
        monkeypatch.setattr(symbolicate_android.shutil, "which", lambda name: "/opt/brew/retrace")
        assert symbolicate_android.find_retrace() == str(sdk)


class TestTheRoute:
    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        monkeypatch.setattr(build_records, "RECORDS_DIR", tmp_path / "records")
        return create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                          enable_crash=False, enable_proxy=False)

    def _post(self, app, body):
        async def go():
            async with AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://test") as c:
                return await c.post("/api/v1/builds/android/record", json=body,
                                    headers={"Authorization": "Bearer k"})
        return asyncio.run(go())

    def test_records(self, app, tmp_path):
        module = _gradle_module(tmp_path, built=datetime.now(UTC))
        r = self._post(app, {"module_path": str(module), "variant": "stagingRelease"})
        assert r.status_code == 200, r.text
        assert r.json()["summary"].startswith("Recorded build")
        assert list((tmp_path / "records").glob("*/record.json"))

    @pytest.mark.parametrize("body, said", [
        ({"module_path": "project/app", "variant": "stagingRelease"}, "must be absolute"),
        ({"module_path": "/nonexistent/app", "variant": "stagingRelease"}, "not a directory"),
        ({"module_path": "/tmp", "variant": "  "}, "variant is empty"),
    ])
    def test_refusals(self, app, body, said):
        r = self._post(app, body)
        assert r.status_code == 400 and said in r.json()["detail"]

    def test_a_variant_not_built_is_404(self, app, tmp_path):
        module = _gradle_module(tmp_path)
        r = self._post(app, {"module_path": str(module), "variant": "prodRelease"})
        assert r.status_code == 404 and "built variants" in r.json()["detail"]


# ── mutation round 2 ─────────────────────────────────────────────────────────


class TestNoRecord:
    def test_an_unknown_source_frame_asks_for_a_record(self, tmp_path, tools):
        """The only mark a real release app's crash carries."""
        report = _java_report()
        top = report.frames[0]
        top.symbol, top.file, top.line = "l82.onClick", "", 539
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", FakeAndroidTools()))
        assert "record it with record_android_build" in report.symbols[0].note
        assert not report.symbolicated

    def test_a_file_merely_named_like_sourcefile_is_no_mark(self, tmp_path, tools):
        report = _java_report(file="SourceFileParser.kt")
        fake = FakeAndroidTools()
        _run([report], symbolicate.SymbolFinder(tmp_path / "records", fake))
        assert report.symbols == [] and fake.calls == []


class TestSettledAgain:
    def test_a_settled_image_without_lines_is_not_asked_again(self, tmp_path, tools):
        """Once resolved a frame has a file and is not sent anyway; one that
        got only a name is not, and must still not be asked twice."""
        fake = FakeAndroidTools(symbolizer="JNI_OnLoad\n??:0:0\n\n")
        finder = symbolicate.SymbolFinder(_recorded(tmp_path), fake)
        report = _native_report()
        _run([report], finder)
        report.symbolicated = False
        _run([report], finder)
        assert len(fake.calls) == 1 and len(report.symbols) == 1


class TestRetraceMismatch:
    def test_an_index_that_was_not_sent_is_not_guessed(self, tmp_path, tools):
        async def odd(argv):
            return 0, "\tat com.example.app.Feed.parse(Feed.kt:12) ~[QUERN-9]\n", ""

        report = _java_report()
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), odd))
        assert report.frames[0].symbol == "a.b.c"
        assert "could not be matched" in report.symbols[0].note
        assert report.symbolicated                   # asking again gets the same answer

    def test_a_passed_through_stamp_is_not_a_file(self, tmp_path, tools):
        report = _java_report(file="r8-map-id-e1ad14241ecb07832d")
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), FakeAndroidTools()))
        assert report.frames[0].file == "" and report.frames[0].line == 3


class TestAppFrameEdges:
    def test_a_native_frame_is_not_flagged_by_the_java_fallback(self, tmp_path, tools):
        """With no frame in the app's package, Java falls back to "not the
        platform's" -- which a native frame's name also is."""
        report = _java_report().model_copy(update={"kind": "anr"})
        report.frames.insert(0, CrashFrame(image="libc.so", offset=0x9e498,
                                           symbol="__epoll_pwait"))
        fake = FakeAndroidTools(retrace={"at a.b.c(SourceFile:3)":
                                         "\tat org.vendor.Feed.parse(Feed.kt:12)"})
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert report.frames[0].app is False
        assert report.app_frame.symbol == "org.vendor.Feed.parse"

    def test_an_app_frame_past_the_cap_stays_the_app_frame(self, tmp_path, tools):
        """It was chosen over the whole trace, most of which is not here."""
        report = _java_report()
        beyond = CrashFrame(symbol="a.d.e", file="SourceFile", line=7, app=True)
        report.app_frame = beyond
        fake = FakeAndroidTools(retrace={
            **RETRACED, "at a.d.e(SourceFile:7)": "\tat com.example.app.Root.cause(Root.kt:3)"})
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert report.app_frame is beyond and beyond.symbol == "com.example.app.Root.cause"
        assert report.frames[0].app                # also the app's, and not chosen


class TestRetentionOfAMappingAlone:
    def test_a_record_with_only_a_mapping_is_counted_and_expired(self, tmp_path):
        root = tmp_path / "records"
        module = _gradle_module(tmp_path)
        for so in module.rglob("*.so"):
            so.unlink()
        records = [_record_android(module, root, now=NOW.replace(minute=m)) for m in range(11)]
        build_records.prune(root, now=NOW.replace(hour=13))
        oldest = next(r for r in build_records.load_all(root) if r.build_id == records[0].build_id)
        assert oldest.dsyms_expired and oldest.mapping == ""


# ── review round 2 ───────────────────────────────────────────────────────────


def _parsed(body: str, kind="crash") -> CrashReport:
    """A report as the DropBox parser builds it: frames capped, the whole
    trace kept, each block's exception line on its first frame."""
    frames, app_frame, _ = android_dropbox._java_trace(body, PKG)
    return CrashReport(
        crash_id="p1", timestamp=NOW, process=PKG, kind=kind,
        frames=frames[:crash_frames.MAX_FRAMES], trace=frames, app_frame=app_frame,
        file_path="dropbox:data_app_crash@2026-09-29 12:00:00", bundle_id=PKG,
        app_version="1.2.3", build_version="99999")


class TestBlocksSurviveFolding:
    def test_an_outline_first_in_a_cause_keeps_the_cause(self, tmp_path, tools):
        """Outlines hold message-building code, so they often open a throwing
        block; folding one merged the cause into the wrapper (measured)."""
        report = _parsed("java.lang.RuntimeException: wrapper\n"
                         "\tat a.w(SourceFile:80)\n"
                         "Caused by: java.lang.IllegalStateException: root\n"
                         "\tat s75.l(SourceFile:4)\n"
                         "\tat a.x(SourceFile:21)\n")
        fake = FakeAndroidTools(retrace={
            "at a.w(SourceFile:80)": "\tat com.example.app.Wrapper.run(Wrapper.kt:5)",
            "at s75.l(SourceFile:4)": "",
            "at a.x(SourceFile:21)": "\tat com.example.app.Cause.boom(Cause.kt:9)"})
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert [f.symbol for f in report.frames] == ["com.example.app.Wrapper.run",
                                                     "com.example.app.Cause.boom"]
        assert report.frames[1].thrown.startswith("Caused by:")
        assert report.app_frame.symbol == "com.example.app.Cause.boom"


class TestInlinedExpansion:
    def test_each_inlined_function_is_a_frame_and_the_app_stays(self, tmp_path, tools):
        """R8 inlines library code into the app's methods; keeping only the
        innermost line dropped the app's frame, and app_frame went to None."""
        report = _parsed("java.lang.IllegalStateException: boom\n"
                         "\tat a.b.c(SourceFile:40)\n"
                         "\tat android.os.Handler.dispatchMessage(Handler.java:106)\n")
        fake = FakeAndroidTools(retrace={"at a.b.c(SourceFile:40)":
                                         "\tat kotlin.time.Clock.now(Clock.kt:3)\n"
                                         "\tat com.example.app.Auth.token(Auth.kt:436)"})
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert [f.symbol for f in report.frames] == [
            "kotlin.time.Clock.now", "com.example.app.Auth.token",
            "android.os.Handler.dispatchMessage"]
        assert report.frames[0].thrown and not report.frames[1].thrown
        assert report.app_frame.symbol == "com.example.app.Auth.token"
        assert "each inlined function is listed" in report.symbols[0].note


class TestAnUnminifiedTrace:
    def test_a_version_match_that_renames_nothing_is_not_applied(self, tmp_path, tools):
        """A kept class maps to itself and is still remapped by line: an
        unminified trace came back with wrong frames, counted resolved."""
        report = _parsed("java.lang.IllegalStateException: boom\n"
                         "\tat com.example.app.Main.onCreate(Main.kt:40)\n")
        fake = FakeAndroidTools(retrace={"at com.example.app.Main.onCreate(Main.kt:40)":
                                         "\tat com.example.app.Main.inject(Main.kt:79)"})
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        f = report.frames[0]
        assert (f.symbol, f.line) == ("com.example.app.Main.onCreate", 40)
        assert "renames no class in this trace" in report.symbols[0].note

    def test_a_d8_apk_does_not_keep_a_leftover_mapping(self, tmp_path):
        module = _gradle_module(tmp_path)
        apk = next(module.rglob("*.apk"))
        with zipfile.ZipFile(apk, "w") as z:
            z.writestr("classes.dex", b'dex\n035\0~~D8{"backend":"dex","compilation-mode":'
                                      b'"debug","min-api":29}')
        record = _record_android(module, tmp_path / "records")
        assert record.mapping == "" and "not minified (D8 built it)" in record.notes[0]

    def test_a_corrupt_apk_does_not_fail_the_record(self, tmp_path):
        module = _gradle_module(tmp_path)
        apk = next(module.rglob("*.apk"))
        with zipfile.ZipFile(apk, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("classes.dex", b"x" * 4096)
        data = bytearray(apk.read_bytes())
        at = data.index(b"classes.dex") + len("classes.dex")   # local header's name
        data[at:at + 8] = b"\xff" * 8                          # into the deflate stream
        apk.write_bytes(bytes(data))
        record = _record_android(module, tmp_path / "records")
        assert record.error == "" and record.mapping_id == "e1ad14241ecb07832d"


class TestNoRecordYet:
    def test_a_trace_read_before_its_record_is_retraced_after(self, tmp_path, tools):
        """A kept source file shows no mark; read before recording it was
        settled with nothing, and never looked at again."""
        root = tmp_path / "records"
        report = _java_report(file="Feed.kt")
        fake = FakeAndroidTools(retrace={"at a.b.c(Feed.kt:3)":
                                         "\tat com.example.app.Feed.parse(Feed.kt:12)"})
        finder = symbolicate.SymbolFinder(root, fake)
        _run([report], finder)
        assert fake.calls == [] and not report.symbolicated
        _record_android(_gradle_module(tmp_path), root)
        _run([report], symbolicate.SymbolFinder(root, fake))
        assert report.frames[0].symbol == "com.example.app.Feed.parse"


class TestCounting:
    def test_frames_that_already_had_lines_are_not_counted(self, tmp_path, tools):
        report = _java_report()
        report.frames += [CrashFrame(symbol=f"android.app.ActivityThread.m{i}",
                                     file="ActivityThread.java", line=100 + i)
                          for i in range(30)]
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path),
                                                FakeAndroidTools(retrace=RETRACED)))
        e = report.symbols[0]
        assert (e.frames_resolved, e.frames_total) == (1, 1)

    def test_a_renamed_class_with_its_minified_line_is_not_resolved(self, tmp_path, tools):
        """retrace renames a class it knows and passes an unknown method's
        minified line through (measured): that line is not a source line."""
        fake = FakeAndroidTools(retrace={"at a.b.c(SourceFile:3)":
                                         "\tat com.example.app.Feed.c(Feed.kt:3)"})
        report = _java_report()
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert report.frames[0].line is None
        assert report.symbols[0].frames_resolved == 0

    def test_a_frame_sent_without_a_line_gets_none_back(self, tmp_path, tools):
        """retrace picks some line for a frame sent without one."""
        report = _java_report()
        report.frames[0].line = None
        fake = FakeAndroidTools(retrace={"at a.b.c(SourceFile)":
                                         "\tat com.example.app.Feed.parse(Feed.kt:2086)"})
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert (report.frames[0].symbol, report.frames[0].line) == (
            "com.example.app.Feed.parse", None)

    def test_r8s_synthetic_class_is_not_a_file(self, tmp_path, tools):
        fake = FakeAndroidTools(retrace={"at a.b.c(SourceFile:3)":
                                         "\tat com.example.app.Feed$1.run(R8$$SyntheticClass:2)"})
        report = _java_report()
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert report.frames[0].file == "" and report.symbols[0].frames_resolved == 0


class TestTheWholeTrace:
    def test_frames_past_the_cap_are_retraced_and_can_be_the_app_frame(self, tmp_path, tools):
        framework = "".join(f"\tat android.app.ActivityThread.m{i}(ActivityThread.java:{i + 1})\n"
                            for i in range(crash_frames.MAX_FRAMES))
        report = _parsed("java.lang.RuntimeException: wrapper\n" + framework
                         + "Caused by: java.lang.NullPointerException\n"
                           "\tat a.x(SourceFile:21)\n")
        assert report.app_frame is None or report.app_frame.symbol == "a.x"
        fake = FakeAndroidTools(retrace={
            "at a.x(SourceFile:21)": "\tat com.example.app.Cause.boom(Cause.kt:9)"})
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), fake))
        assert "at a.x(SourceFile:21)" in fake.sent
        assert report.app_frame.symbol == "com.example.app.Cause.boom"
        assert len(report.frames) == crash_frames.MAX_FRAMES


class TestUntrustworthyOutput:
    def test_a_platform_frame_missing_from_the_output_is_not_trusted(self, tmp_path, tools):
        """A retrace that wrote no tags would make every frame look folded."""
        async def untagged(argv):
            return 0, ("\tat com.example.app.Feed.parse(Feed.kt:12) ~[QUERN-0]\n"
                       "\tat android.os.Handler.dispatchMessage(Handler.java:106)\n"), ""

        report = _java_report()
        _run([report], symbolicate.SymbolFinder(_recorded(tmp_path), untagged))
        assert report.frames[0].symbol == "a.b.c" and len(report.frames) == 2
        assert "could not be matched" in report.symbols[0].note


class TestBlockAlignment:
    def test_a_line_the_parser_skips_does_not_shift_the_blocks(self):
        frames, app_frame, _ = android_dropbox._java_trace(
            "java.lang.RuntimeException: outer\n"
            "\tat not a frame line\n"
            "\tat com.example.app.Outer.run(Outer.kt:1)\n"
            "Caused by: java.lang.IllegalStateException: inner\n"
            "\tat com.example.app.Inner.boom(Inner.kt:2)\n", PKG)
        assert [f.thrown.split(":")[0] for f in frames] == [
            "java.lang.RuntimeException", "Caused by"]
        assert app_frame.symbol == "com.example.app.Inner.boom"
