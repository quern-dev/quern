"""Crash frames and images, kept as the report gives them (#326).

The .ips fixtures are real reports -- a Debug build of an app, on a simulator
and on an iPhone 12 (iOS 26.5.2) -- with the app's name, bundle id, UUIDs and
paths replaced; Apple's own frames are as they came.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from server.sources import crash_frames
from server.sources.crash import CrashAdapter
from server.sources.crash_frames import format_frame, java_frames, native_frames

FIXTURES = Path(__file__).parent / "fixtures"


def _ips(name):
    header, body = (FIXTURES / "crash_ips" / f"{name}.ips").read_text().split("\n", 1)
    return json.loads(header), json.loads(body)


def _write(tmp_path, header, body, name="MyApp-2026-09-28-080308.ips"):
    f = tmp_path / name
    f.write_text(json.dumps(header) + "\n" + json.dumps(body))
    return f


def _parse(path):
    return CrashAdapter(watch_dir=path.parent)._parse_crash_file(path, path.read_text())


class TestIpsFromTheSimulator:
    """macOS resolves a simulator app's frames to function and source line."""

    def test_where_in_the_app_it_crashed_keeps_its_source_line(self):
        report = _parse(FIXTURES / "crash_ips" / "simulator_debug.ips")
        frame = report.app_frame
        assert (frame.image, frame.symbol, frame.symbol_offset) == (
            "MyApp.debug.dylib", "__debug_main_executable_dylib_entry_point", 64)
        assert (frame.file, frame.line) == ("AppDelegate.swift", 13)
        assert format_frame(frame) == (
            "MyApp.debug.dylib: __debug_main_executable_dylib_entry_point + 64"
            " (AppDelegate.swift:13)")

    def test_the_app_and_its_build(self):
        report = _parse(FIXTURES / "crash_ips" / "simulator_debug.ips")
        assert (report.bundle_id, report.app_version, report.build_version) == (
            "com.example.myapp", "1.2.3", "42")

    def test_the_log_line_says_where_in_the_app(self):
        report = _parse(FIXTURES / "crash_ips" / "simulator_debug.ips")
        assert CrashAdapter._crash_summary(report).endswith(
            "in MyApp.debug.dylib: __debug_main_executable_dylib_entry_point + 64"
            " (AppDelegate.swift:13)")


class TestIpsFromAPhone:
    """A Debug build is not stripped, so the phone names the function; the
    source line needs the build's debug info on the Mac (#326, step 3)."""

    def test_the_function_is_named_but_not_the_line(self):
        report = _parse(FIXTURES / "crash_ips" / "device_debug.ips")
        assert report.app_frame.symbol == "__debug_main_executable_dylib_entry_point"
        assert report.app_frame.file == "" and report.app_frame.line is None

    def test_every_frame_keeps_its_image_and_offset(self):
        report = _parse(FIXTURES / "crash_ips" / "device_debug.ips")
        assert len(report.frames) == 12
        assert all(f.image and f.offset is not None for f in report.frames)
        assert report.top_frames[0] == "libsystem_kernel.dylib: mach_msg2_trap + 8"

    def test_the_images_carry_what_a_symbolicator_needs(self):
        """UUID to find the binary, load address to turn an address into an offset."""
        report = _parse(FIXTURES / "crash_ips" / "device_debug.ips")
        app = next(i for i in report.images if i.name == "MyApp.debug.dylib")
        assert app.uuid and app.base and app.arch == "arm64"
        assert app.path.endswith("/MyApp.app/MyApp.debug.dylib")
        # Only the images the frames point into, once each.
        assert sorted(i.name for i in report.images) == sorted({f.image for f in report.frames})

    def test_os_frames_are_not_the_apps(self):
        report = _parse(FIXTURES / "crash_ips" / "device_debug.ips")
        assert {f.image for f in report.frames if f.app} == {"MyApp.debug.dylib"}


class TestIpsShapes:
    def test_a_stripped_build_keeps_the_offset_to_resolve_later(self, tmp_path):
        """A Release build has no symbols on the phone: the frame was a bare
        number with no binary, so nothing could ever resolve it."""
        header, body = _ips("device_debug")
        for frame in body["threads"][0]["frames"]:
            if body["usedImages"][frame["imageIndex"]]["name"] == "MyApp.debug.dylib":
                del frame["symbol"], frame["symbolLocation"]
        report = _parse(_write(tmp_path, header, body))
        frame = report.app_frame
        assert frame.symbol == "" and frame.offset == 2813080
        assert format_frame(frame) == "MyApp.debug.dylib: 0x2aec98"

    def test_the_first_app_frame_is_where_it_crashed(self, tmp_path):
        """A real crash in the app's code has app frames above main: the
        innermost one is the answer, not the entry point."""
        header, body = _ips("simulator_debug")
        app_index = next(i for i, img in enumerate(body["usedImages"])
                         if img["name"] == "MyApp.debug.dylib")
        body["threads"][0]["frames"][:0] = [
            {"imageIndex": 0, "imageOffset": 4012, "symbol": "__pthread_kill", "symbolLocation": 8},
            {"imageIndex": app_index, "imageOffset": 81234, "symbol": "CacheList.load()",
             "symbolLocation": 120, "sourceFile": "CacheList.swift", "sourceLine": 88},
        ]
        report = _parse(_write(tmp_path, header, body))
        assert format_frame(report.app_frame) == (
            "MyApp.debug.dylib: CacheList.load() + 120 (CacheList.swift:88)")

    def test_an_offset_of_zero_is_a_frame(self, tmp_path):
        """`elif image:` dropped it, and the frames after it shifted up."""
        header, body = _ips("device_debug")
        body["threads"][0]["frames"][0] = {"imageIndex": 0, "imageOffset": 0}
        report = _parse(_write(tmp_path, header, body))
        assert report.frames[0].offset == 0 and len(report.frames) == 12

    def test_no_app_frame_falls_back_to_the_top_frame(self, tmp_path):
        header, body = _ips("device_debug")
        body["threads"][0]["frames"] = [f for f in body["threads"][0]["frames"]
                                        if body["usedImages"][f["imageIndex"]]["name"]
                                        != "MyApp.debug.dylib"]
        report = _parse(_write(tmp_path, header, body))
        assert report.app_frame is None
        assert "@ libsystem_kernel.dylib: mach_msg2_trap + 8" in CrashAdapter._crash_summary(report)

    def test_no_faulting_index_uses_the_triggered_thread(self, tmp_path):
        header, body = _ips("device_debug")
        body["threads"].insert(0, {"frames": []})
        del body["faultingThread"]
        report = _parse(_write(tmp_path, header, body))
        assert len(report.frames) == 12

    def test_a_bad_image_index_is_a_frame_without_an_image(self, tmp_path):
        header, body = _ips("device_debug")
        body["threads"][0]["frames"][0]["imageIndex"] = 999
        report = _parse(_write(tmp_path, header, body))
        assert report.frames[0].image == "" and len(report.frames) == 12

    def test_an_embedded_framework_is_the_apps(self, tmp_path):
        """Inside the app's bundle, named after nothing in particular."""
        header, body = _ips("device_debug")
        app_dir = body["procPath"].rsplit("/", 1)[0]
        body["usedImages"].append({"name": "Vendor", "arch": "arm64", "base": 4400000000,
                                   "uuid": "11111111-2222-3333-4444-555555555555",
                                   "path": f"{app_dir}/Frameworks/Vendor.framework/Vendor"})
        body["threads"][0]["frames"].insert(0, {
            "imageIndex": len(body["usedImages"]) - 1, "imageOffset": 512,
            "symbol": "Vendor.explode()", "symbolLocation": 4})
        report = _parse(_write(tmp_path, header, body))
        assert report.app_frame.image == "Vendor"

    def test_paths_elided_the_app_is_known_by_its_name(self, tmp_path):
        header, body = _ips("device_debug")
        for img in body["usedImages"]:
            img["path"] = ""
        report = _parse(_write(tmp_path, header, body))
        assert report.app_frame is not None and report.app_frame.image == "MyApp.debug.dylib"

    def test_a_deep_stack_is_capped(self, tmp_path):
        header, body = _ips("device_debug")
        frames = body["threads"][0]["frames"]
        body["threads"][0]["frames"] = [copy.deepcopy(frames[0]) for _ in range(100)]
        report = _parse(_write(tmp_path, header, body))
        assert len(report.frames) == crash_frames.MAX_FRAMES
        assert len(report.top_frames) == crash_frames.TOP_FRAMES


class TestCrashText:
    """iOS 14 and older: the image name and address were dropped."""

    def test_frames_keep_their_image_and_the_binary_images_are_read(self):
        report = _parse(FIXTURES / "crash_sample.crash")
        assert format_frame(report.app_frame) == (
            "MyApp: -[FeedViewController tableView:cellForRowAtIndexPath:] + 128")
        [image] = [i for i in report.images if i.name == "MyApp"]
        assert (image.uuid, image.base, image.arch) == (
            "AABB1122334455667788990011223344", 0x100000000, "arm64")
        # 0x100abc000 into an image loaded at 0x100000000.
        assert report.app_frame.offset == 0xabc000
        assert (report.bundle_id, report.app_version, report.build_version) == (
            "com.example.myapp", "1.0.0", "100")

    def test_an_unsymbolicated_line_keeps_the_offset(self, tmp_path):
        text = (FIXTURES / "crash_sample.crash").read_text().replace(
            "0x0000000100abc000 -[FeedViewController tableView:cellForRowAtIndexPath:] + 128",
            "0x0000000100abc000 0x100000000 + 11255808")
        f = tmp_path / "MyApp.crash"
        f.write_text(text)
        report = _parse(f)
        assert report.app_frame.symbol == "" and report.app_frame.offset == 11255808
        assert format_frame(report.app_frame) == "MyApp: 0xabc000"

    def test_an_unsymbolicated_line_without_binary_images(self, tmp_path):
        """The offset is on the line; with no Binary Images section, there is
        no load address to compute it from."""
        text = (FIXTURES / "crash_sample.crash").read_text().replace(
            "0x0000000100abc000 -[FeedViewController tableView:cellForRowAtIndexPath:] + 128",
            "0x0000000100abc000 0x100000000 + 11255808").split("Binary Images:")[0]
        f = tmp_path / "MyApp.crash"
        f.write_text(text)
        assert _parse(f).app_frame.offset == 11255808

    def test_a_symbol_with_its_source_line(self, tmp_path):
        text = (FIXTURES / "crash_sample.crash").read_text().replace(
            "-[FeedViewController tableView:cellForRowAtIndexPath:] + 128",
            "FeedViewController.cell(for:) + 128 (FeedViewController.swift:42)")
        f = tmp_path / "MyApp.crash"
        f.write_text(text)
        frame = _parse(f).app_frame
        assert (frame.symbol, frame.file, frame.line) == (
            "FeedViewController.cell(for:)", "FeedViewController.swift", 42)


class TestAndroidNative:
    def test_a_cpp_symbol_with_its_own_parentheses(self):
        [frame] = native_frames([
            "      #02 pc 000000000001650c  /system/lib64/libutils.so "
            "(android::Looper::pollOnce(int, int*, int*, void**)+112) (BuildId: c1f7ebcd)",
        ])
        assert (frame.image, frame.offset, frame.symbol, frame.symbol_offset, frame.build_id) == (
            "libutils.so", 0x1650c, "android::Looper::pollOnce(int, int*, int*, void**)",
            112, "c1f7ebcd")
        assert not frame.app

    def test_a_cpp_operator_with_a_plus_in_its_name(self):
        """The offset follows the last "+", not the first."""
        [frame] = native_frames([
            "#03 pc 0000000000009abc  /system/lib64/libc++.so "
            "(std::__1::basic_string<char>::operator+=(char const*)+24) (BuildId: 1234)",
        ])
        assert (frame.symbol, frame.symbol_offset) == (
            "std::__1::basic_string<char>::operator+=(char const*)", 24)

    def test_the_apps_library_is_the_apps(self):
        [frame] = native_frames([
            "#00 pc 0000000000001234  /data/app/~~x==/com.example.app-y==/lib/arm64/libfoo.so "
            "(Foo::crash()+12) (BuildId: abcd)",
        ])
        assert frame.app and frame.image == "libfoo.so"

    def test_a_library_inside_the_apk(self):
        [frame] = native_frames([
            "#00 pc 0000000000001234  /data/app/com.example.app/base.apk!libfoo.so "
            "(offset 0x1000) (Foo::crash()+12) (BuildId: abcd)",
        ])
        assert (frame.image, frame.symbol, frame.symbol_offset) == ("libfoo.so", "Foo::crash()", 12)

    def test_an_unsymbolized_frame(self):
        [frame] = native_frames(["#00 pc 0000000000001234  /data/app/x/lib/arm64/libfoo.so"])
        assert frame.symbol == "" and frame.offset == 0x1234

    def test_only_the_crashing_threads_backtrace(self):
        """The tombstone lists every thread's; reading the whole record mixed
        another thread's frames into the crash."""
        from server.sources.android_dropbox import device_zone, parse_dropbox

        text = (FIXTURES / "android_dropbox" / "system_app_native_crash.dropbox").read_text()
        text = text.rstrip("\n") + (
            "\n\n--- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---\n"
            "pid: 1, tid: 2, name: OtherThread\nbacktrace:\n"
            "      #00 pc 0000000000000abc  /system/lib64/libother.so (other+4)\n"
        )
        [report] = parse_dropbox(text, serial="s", zone=device_zone("America/Los_Angeles", ""))
        assert len(report.frames) == 22
        assert "libother.so" not in {f.image for f in report.frames}
        build_ids = {i.name: i.uuid for i in report.images}
        assert build_ids["libc.so"] == "cd7952cb40d1a2deca6420c2da7910be"


class TestAndroidJava:
    def test_file_and_line(self):
        [frame] = java_frames(["at com.example.app.Foo.bar(Foo.java:42)"])
        assert (frame.symbol, frame.file, frame.line, frame.app) == (
            "com.example.app.Foo.bar", "Foo.java", 42, True)

    @pytest.mark.parametrize("where", ["Native Method", "Unknown Source", "Unknown Source:3"])
    def test_no_source(self, where):
        [frame] = java_frames([f"at com.example.app.Foo.bar({where})"])
        assert frame.file == "" and frame.line is None

    @pytest.mark.parametrize("symbol", [
        "android.app.ActivityThread.main", "java.lang.reflect.Method.invoke",
        "androidx.fragment.app.Fragment.performCreate", "kotlin.coroutines.Foo.bar",
        "com.android.internal.os.ZygoteInit.main",
    ])
    def test_the_platforms_frames_are_not_the_apps(self, symbol):
        [frame] = java_frames([f"at {symbol}(X.java:1)"])
        assert not frame.app

    def test_a_crash_through_the_framework_names_no_app_frame(self):
        from server.sources.android_dropbox import device_zone, parse_dropbox

        text = (FIXTURES / "android_dropbox" / "system_app_crash.dropbox").read_text()
        report = parse_dropbox(text, serial="s", zone=device_zone("America/Los_Angeles", ""))[0]
        assert report.app_frame is None               # am crash: all framework code
        assert report.frames[0].file == "ActivityThread.java"


def test_the_log_line_names_a_signal_once():
    from server.models import CrashReport

    report = CrashReport(crash_id="x", timestamp="2026-09-28T00:00:00Z", process="app",
                         exception_type="signal 11 (SIGSEGV)", signal="SIGSEGV")
    assert CrashAdapter._crash_summary(report) == "CRASH: app signal 11 (SIGSEGV)"
    report.exception_type = "EXC_BAD_ACCESS"
    assert CrashAdapter._crash_summary(report) == "CRASH: app EXC_BAD_ACCESS (SIGSEGV)"
