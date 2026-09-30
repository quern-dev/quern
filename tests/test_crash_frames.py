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


class TestARealCrash:
    """A Swift fatalError in a real app's Debug build on a simulator
    (EXC_BREAKPOINT), with the app's names replaced. Swift's
    _assertionFailure is on top; the app's own frame is right under it."""

    def test_where_in_the_app_it_crashed(self):
        report = _parse(FIXTURES / "crash_ips" / "simulator_fatal_error.ips")
        assert format_frame(report.app_frame) == (
            "MyApp.debug.dylib: closure #1 in SettingsPresenter.resetStore() + 412"
            " (SettingsPresenter.swift:368)")
        assert report.frames[0].symbol == "_assertionFailure(_:_:file:line:flags:)"
        assert not report.frames[0].app
        assert (report.exception_type, report.signal) == ("EXC_BREAKPOINT", "SIGTRAP")
        assert report.killed_by == "" and report.frames_from == "crashing_thread"

    def test_the_app_and_its_build(self):
        report = _parse(FIXTURES / "crash_ips" / "simulator_fatal_error.ips")
        assert (report.bundle_id, report.app_version, report.build_version) == (
            "com.example.myapp", "1.2.3", "42")

    def test_the_log_line_says_where_in_the_app(self):
        report = _parse(FIXTURES / "crash_ips" / "simulator_fatal_error.ips")
        assert CrashAdapter._crash_summary(report) == (
            "CRASH: MyApp EXC_BREAKPOINT (SIGTRAP) in MyApp.debug.dylib: closure #1 in "
            "SettingsPresenter.resetStore() + 412 (SettingsPresenter.swift:368)")

    def test_a_swift_fatal_error_has_no_reason_in_the_report(self):
        """Its message goes to the app's log. Saying so beats inventing one."""
        assert _parse(FIXTURES / "crash_ips" / "simulator_fatal_error.ips").reason == ""


class TestKilledByAnotherProcess:
    """Both .ips fixtures below are the app stopped by a signal from outside:
    from a shell on the simulator, through devicectl on the phone. Their
    frames say where it was idle -- the app's entry point under the run loop
    -- and presenting that as where it crashed sent the reader to
    AppDelegate.swift line 13."""

    def test_the_simulator_kill_names_the_killer_and_no_crash_site(self):
        report = _parse(FIXTURES / "crash_ips" / "simulator_debug.ips")
        assert report.killed_by == "zsh"
        assert report.app_frame is None
        assert CrashAdapter._crash_summary(report).endswith("(killed by zsh)")

    def test_the_phone_kill_through_devicectl(self):
        report = _parse(FIXTURES / "crash_ips" / "device_debug.ips")
        assert report.killed_by == "dtappserviced" and report.app_frame is None

    def test_a_kill_is_no_crash_site_even_inside_the_apps_code(self, tmp_path):
        """Stopped mid-work, its frames can be deep in the app -- still not a
        crash site."""
        header, body = _ips("simulator_fatal_error")
        body["termination"].update(byProc="zsh", byPid=body["pid"] + 7)
        report = _parse(_write(tmp_path, header, body))
        assert report.killed_by == "zsh" and report.app_frame is None

    def test_the_frames_are_still_kept(self):
        """Where it was is still worth having; it is only not the crash site."""
        report = _parse(FIXTURES / "crash_ips" / "simulator_debug.ips")
        entry = next(f for f in report.frames if f.app)
        assert (entry.symbol, entry.file, entry.line) == (
            "__debug_main_executable_dylib_entry_point", "AppDelegate.swift", 13)


def _image_index(body, name):
    return next(i for i, img in enumerate(body["usedImages"]) if img["name"] == name)


def _self_crash(name):
    """A fixture made into a crash the app raised itself, as a real one reads:
    the signal from the app's own pid."""
    header, body = _ips(name)
    body["termination"]["byProc"] = "exc handler"
    body["termination"]["byPid"] = body["pid"]
    return header, body


class TestIpsFromAPhone:
    """A Debug build is not stripped, so the phone names functions; source
    lines need the build's debug info on the Mac (#326, step 3)."""

    def test_every_frame_keeps_its_image_and_offset(self):
        report = _parse(FIXTURES / "crash_ips" / "device_debug.ips")
        assert len(report.frames) == 12
        assert all(f.image and f.offset is not None for f in report.frames)
        assert all(f.file == "" for f in report.frames)
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


class TestWhichFrame:
    def test_the_entry_point_under_the_run_loop_is_not_the_crash_site(self, tmp_path):
        """A crash that never reached the app's own code is not "in main"."""
        header, body = _self_crash("device_debug")
        report = _parse(_write(tmp_path, header, body))
        assert report.app_frame is None
        assert "@ libsystem_kernel.dylib: mach_msg2_trap + 8" in CrashAdapter._crash_summary(report)

    def test_a_swift_main_under_the_run_loop_is_an_entry_point(self):
        from server.models import CrashFrame

        run_loop = CrashFrame(image="UIKitCore", symbol="UIApplicationMain")
        main = CrashFrame(image="MyApp", symbol="static MyApp.$main()", app=True)
        assert crash_frames.first_app_frame([run_loop, main]) is None

    def test_main_is_the_crash_site_when_it_crashed_there(self):
        """An abort() from main itself (measured, from a real probe report):
        with no run loop above it, main is where it went wrong."""
        from server.models import CrashFrame

        abort = CrashFrame(image="libsystem_c.dylib", symbol="abort")
        main = CrashFrame(image="probe", symbol="main", file="a.c", line=2, app=True)
        assert crash_frames.first_app_frame([abort, main]) is main

    def test_a_signal_handlers_frames_are_skipped(self, tmp_path):
        """A crash reporter linked into the app runs its handler on the
        crashing thread, above _sigtramp: its frames are the app's binary but
        not where the app went wrong."""
        header, body = _ips("simulator_fatal_error")
        app = _image_index(body, "MyApp.debug.dylib")
        system = body["threads"][0]["frames"][0]["imageIndex"]
        body["threads"][0]["frames"][:0] = [
            {"imageIndex": app, "imageOffset": 10, "symbol": "ReporterSignalHandler",
             "symbolLocation": 4},
            {"imageIndex": system, "imageOffset": 20, "symbol": "_sigtramp", "symbolLocation": 56},
        ]
        report = _parse(_write(tmp_path, header, body))
        assert report.app_frame.symbol == "closure #1 in SettingsPresenter.resetStore()"

    def test_an_uncaught_exception_uses_its_own_backtrace(self, tmp_path):
        """Its crashing thread is only abort under the run loop; the throw site
        and the reason are in lastExceptionBacktrace and asi."""
        header, body = _self_crash("device_debug")
        app = _image_index(body, "MyApp.debug.dylib")
        body["lastExceptionBacktrace"] = [
            {"imageIndex": 1, "imageOffset": 1000, "symbol": "__exceptionPreprocess",
             "symbolLocation": 164},
            {"imageIndex": app, "imageOffset": 81234, "symbol": "CacheList.load()",
             "symbolLocation": 120},
        ]
        body["asi"] = {"CoreFoundation": [
            "*** Terminating app due to uncaught exception 'NSRangeException', "
            "reason: 'index 3 beyond bounds'"]}
        report = _parse(_write(tmp_path, header, body))
        assert report.frames_from == "exception"
        assert report.app_frame.symbol == "CacheList.load()"
        assert report.reason.startswith(
            "*** Terminating app due to uncaught exception 'NSRangeException'")
        assert len(report.frames) == 2

    def test_a_stripped_build_keeps_the_offset_to_resolve_later(self, tmp_path):
        """A Release build has no symbols on the phone: the frame was a bare
        number with no binary, so nothing could ever resolve it."""
        header, body = _self_crash("simulator_fatal_error")
        for frame in body["threads"][0]["frames"]:
            if body["usedImages"][frame["imageIndex"]]["name"] == "MyApp.debug.dylib":
                for k in ("symbol", "symbolLocation", "sourceFile", "sourceLine"):
                    frame.pop(k, None)
        report = _parse(_write(tmp_path, header, body))
        frame = report.app_frame
        assert frame.symbol == "" and frame.offset is not None
        assert format_frame(frame) == f"MyApp.debug.dylib: 0x{frame.offset:x}"

    def test_an_offset_of_zero_is_a_frame(self, tmp_path):
        """`elif image:` dropped it, and the frames after it shifted up."""
        header, body = _ips("device_debug")
        body["threads"][0]["frames"][0] = {"imageIndex": 0, "imageOffset": 0}
        report = _parse(_write(tmp_path, header, body))
        assert report.frames[0].offset == 0 and len(report.frames) == 12

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
        header, body = _self_crash("device_debug")
        app_dir = body["procPath"].rsplit("/", 1)[0]
        body["usedImages"].append({"name": "Vendor", "arch": "arm64", "base": 4400000000,
                                   "uuid": "11111111-2222-3333-4444-555555555555",
                                   "path": f"{app_dir}/Frameworks/Vendor.framework/Vendor"})
        body["threads"][0]["frames"].insert(0, {
            "imageIndex": len(body["usedImages"]) - 1, "imageOffset": 512,
            "symbol": "Vendor.explode()", "symbolLocation": 4})
        report = _parse(_write(tmp_path, header, body))
        assert report.app_frame.image == "Vendor"

    def test_an_extension_counts_its_host_apps_frameworks(self, tmp_path):
        header, body = _self_crash("device_debug")
        app_dir = body["procPath"].rsplit("/", 1)[0]
        body["procPath"] = f"{app_dir}/PlugIns/Widget.appex/Widget"
        body["procName"] = "Widget"
        body["usedImages"].append({"name": "Shared", "arch": "arm64", "base": 4400000000,
                                   "path": f"{app_dir}/Frameworks/Shared.framework/Shared"})
        body["threads"][0]["frames"].insert(0, {
            "imageIndex": len(body["usedImages"]) - 1, "imageOffset": 8, "symbol": "Shared.f()"})
        assert _parse(_write(tmp_path, header, body)).app_frame.image == "Shared"

    def test_a_daemon_outside_an_app_bundle_has_no_app(self, tmp_path):
        """Without the .app check, a daemon at /usr/libexec/foo made every
        image beside it the app's."""
        header, body = _self_crash("device_debug")
        body["procPath"] = "/usr/libexec/foo"
        body["procName"] = "foo"
        first = body["usedImages"][body["threads"][0]["frames"][0]["imageIndex"]]
        first["path"] = "/usr/libexec/libneighbour.dylib"
        report = _parse(_write(tmp_path, header, body))
        assert not report.frames[0].app

    def test_a_bundle_prefix_is_not_the_bundle(self, tmp_path):
        """`/…/MyApp.app2/lib` is not inside `/…/MyApp.app`."""
        header, body = _self_crash("device_debug")
        app_dir = body["procPath"].rsplit("/", 1)[0]
        first = body["usedImages"][body["threads"][0]["frames"][0]["imageIndex"]]
        first["path"] = app_dir + "2/libneighbour.dylib"
        first["name"] = "libneighbour.dylib"
        report = _parse(_write(tmp_path, header, body))
        assert not report.frames[0].app

    def test_paths_elided_the_app_is_known_by_its_name(self, tmp_path):
        header, body = _ips("device_debug")
        for img in body["usedImages"]:
            img["path"] = ""
        report = _parse(_write(tmp_path, header, body))
        assert {f.image for f in report.frames if f.app} == {"MyApp.debug.dylib"}

    def test_the_bundle_info_when_the_header_lacks_it(self, tmp_path):
        header, body = _ips("device_debug")
        for k in ("bundleID", "app_version", "build_version"):
            header.pop(k)
        report = _parse(_write(tmp_path, header, body))
        assert (report.bundle_id, report.app_version, report.build_version) == (
            "com.example.myapp", "1.2.3", "42")

    def test_a_deep_stack_is_capped(self, tmp_path):
        header, body = _ips("device_debug")
        frames = body["threads"][0]["frames"]
        body["threads"][0]["frames"] = [copy.deepcopy(frames[0]) for _ in range(100)]
        report = _parse(_write(tmp_path, header, body))
        assert len(report.frames) == crash_frames.MAX_FRAMES
        assert len(report.top_frames) == crash_frames.TOP_FRAMES

    @pytest.mark.parametrize("mangle", [
        lambda b: b["usedImages"][0].update(name=123, path=["x"], uuid={}, arch=4),
        lambda b: b.update(procPath=42),
        lambda b: b["threads"][0]["frames"][0].update(sourceFile=7, symbol=8),
        lambda b: b.update(faultingThread=True),
    ])
    def test_odd_field_types_cost_detail_not_the_report(self, tmp_path, mangle):
        """These made a report that used to parse vanish, with only a server
        log line to say so."""
        header, body = _ips("device_debug")
        mangle(body)
        header["bundleID"] = ["not", "text"]
        report = _parse(_write(tmp_path, header, body))
        assert report is not None and report.process == "MyApp"
        # And the frames themselves survive: one odd field costs that field.
        assert len(report.frames) == 12

    def test_frames_that_cannot_be_read_cost_the_frames_not_the_report(
        self, tmp_path, monkeypatch,
    ):
        def broken(data):
            raise TypeError("a shape nobody has seen")

        monkeypatch.setattr(crash_frames, "ips_frames", broken)
        report = _parse(FIXTURES / "crash_ips" / "simulator_fatal_error.ips")
        assert report is not None and report.frames == [] and report.app_frame is None
        assert report.exception_type == "EXC_BREAKPOINT"

    def test_a_boolean_faulting_thread_is_not_thread_one(self, tmp_path):
        header, body = _ips("device_debug")
        body["threads"].append({"frames": [{"imageIndex": 0, "imageOffset": 1}]})
        body["faultingThread"] = True                  # True == 1 to Python
        report = _parse(_write(tmp_path, header, body))
        assert len(report.frames) == 12               # the triggered thread, 0

    def test_a_nested_app_counts_as_the_outer_one(self, tmp_path):
        """A watch app inside the phone app: its frameworks and the phone
        app's are one app's code."""
        header, body = _self_crash("device_debug")
        app_dir = body["procPath"].rsplit("/", 1)[0]
        body["procPath"] = f"{app_dir}/Watch/Face.app/Face"
        body["procName"] = "Face"
        body["usedImages"].append({"name": "Shared", "arch": "arm64", "base": 4400000000,
                                   "path": f"{app_dir}/Frameworks/Shared.framework/Shared"})
        body["threads"][0]["frames"].insert(0, {
            "imageIndex": len(body["usedImages"]) - 1, "imageOffset": 8, "symbol": "Shared.f()"})
        assert _parse(_write(tmp_path, header, body)).app_frame.image == "Shared"


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

    def test_the_crash_site_past_the_frame_cap(self, tmp_path):
        """Picked before the frames are capped, as in an `.ips`, and its image
        kept."""
        deep = "".join(f"{i}   UIKitCore                   0x00000001abcd{i:04x} "
                       f"-[UIView layout{i}] + 4\n" for i in range(35))
        text = (FIXTURES / "crash_sample.crash").read_text().replace(
            "Thread 0 Crashed:\n", "Thread 0 Crashed:\n" + deep, 1)
        text = text.rstrip("\n") + (
            "\n0x1abc00000 -        0x1abffffff UIKitCore arm64  <11> /System/UIKitCore"
            "\n0x1cde00000 - 0x1cdffffff CoreFoundation arm64  <22> /System/CoreFoundation\n")
        f = tmp_path / "MyApp.crash"
        f.write_text(text)
        report = _parse(f)
        assert report.app_frame.symbol == "-[FeedViewController tableView:cellForRowAtIndexPath:]"
        assert len(report.frames) == crash_frames.MAX_FRAMES
        # CoreFoundation's frames all lie past the cap.
        assert {i.name for i in report.images} == {"MyApp", "UIKitCore"}

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

    def test_the_bundle_path_decides_what_is_the_apps(self, tmp_path):
        """With real paths, not the name fallback: a framework inside the bundle
        is the app's, one outside it is not."""
        text = (FIXTURES / "crash_sample.crash").read_text()
        app = "/private/var/containers/Bundle/Application/X/MyApp.app"
        text = text.replace(
            "Path:                /private/var/containers/Bundle/Application/.../MyApp.app/MyApp",
            f"Path:                {app}/MyApp")
        text = text.replace(
            "0   MyApp                       0x0000000100abc000",
            "0   Kit                         0x0000000100abc000")
        text = text.replace("<AABB1122334455667788990011223344> /private/var/.../MyApp",
                            f"<AABB1122334455667788990011223344> {app}/MyApp") + (
            "0x100000000 -        0x100ffffff Kit arm64  <CCDD1122334455667788990011223344> "
            f"{app}/Frameworks/Kit.framework/Kit\n")
        f = tmp_path / "MyApp.crash"
        f.write_text(text)
        assert _parse(f).app_frame.image == "Kit"

    def test_an_image_name_with_spaces(self, tmp_path):
        """The old split kept it; a `\\S+` image name dropped the whole frame."""
        text = (FIXTURES / "crash_sample.crash").read_text().replace(
            "0   MyApp                       0x0000000100abc000",
            "0   My App                      0x0000000100abc000",
        ).replace("Process:             MyApp", "Process:             My App")
        f = tmp_path / "MyApp.crash"
        f.write_text(text)
        assert _parse(f).app_frame.image == "My App"

    def test_the_macos_style_unsymbolicated_line(self, tmp_path):
        """`MyApp + 11255808`: an image and an offset, not a function named MyApp."""
        text = (FIXTURES / "crash_sample.crash").read_text().replace(
            "0x0000000100abc000 -[FeedViewController tableView:cellForRowAtIndexPath:] + 128",
            "0x0000000100abc000 MyApp + 11255808")
        f = tmp_path / "MyApp.crash"
        f.write_text(text)
        frame = _parse(f).app_frame
        assert frame.symbol == "" and frame.offset == 11255808

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

    def test_a_jit_frame(self):
        """`/memfd:jit-cache (deleted)`: a path with a bracket of its own."""
        [frame] = native_frames([
            "#04 pc 0000000000123456  /memfd:jit-cache (deleted) (offset 0x2000000) "
            "(com.example.Foo.bar+300)",
        ])
        assert (frame.image, frame.symbol, frame.symbol_offset) == (
            "memfd:jit-cache (deleted)", "com.example.Foo.bar", 300)

    def test_a_jit_frame_without_a_symbol(self):
        [frame] = native_frames(["#04 pc 0000000000123456  /memfd:jit-cache (deleted)"])
        assert frame.symbol == "" and frame.image == "memfd:jit-cache (deleted)"

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
        libc = next(i for i in report.images if i.name == "libc.so")
        assert libc.uuid == "cd7952cb40d1a2deca6420c2da7910be"
        assert libc.path == "/apex/com.android.runtime/lib64/bionic/libc.so"


    def _native(self, text):
        from server.sources.android_dropbox import device_zone, parse_dropbox

        [report] = parse_dropbox(text, serial="s", zone=device_zone("America/Los_Angeles", ""))
        return report

    def test_a_line_that_is_not_a_frame_leaves_the_paths_in_step(self):
        """Each image's path comes from its own frame's line: pairing frames
        with lines by position put every image after a skipped line on the
        next line's path."""
        text = (FIXTURES / "android_dropbox" / "system_app_native_crash.dropbox").read_text()
        text = text.replace("      #01 pc 0000000000016628",
                            "      #00 pc (unreadable)\n      #01 pc 0000000000016628", 1)
        paths = {i.name: i.path for i in self._native(text).images}
        assert paths["libutils.so"] == "/system/lib64/libutils.so"
        assert paths["libandroid_runtime.so"] == "/system/lib64/libandroid_runtime.so"

    def test_two_libraries_of_one_name_are_both_kept(self):
        """An app's own libcrypto.so beside the system's: keyed by name, the
        second lost its path and BuildId."""
        text = (FIXTURES / "android_dropbox" / "system_app_native_crash.dropbox").read_text()
        own = ("      #00 pc 0000000000001000  /data/app/~~x/com.example-1/lib/arm64/"
               "libcrypto.so (EVP_Digest+8) (BuildId: aaaa)\n"
               "      #01 pc 0000000000002000  /system/lib64/libcrypto.so (SHA256+4) "
               "(BuildId: bbbb)\n")
        report = self._native(text.replace("backtrace:\n", "backtrace:\n" + own, 1))
        crypto = sorted((i.uuid, i.path) for i in report.images if i.name == "libcrypto.so")
        assert crypto == [("aaaa", "/data/app/~~x/com.example-1/lib/arm64/libcrypto.so"),
                          ("bbbb", "/system/lib64/libcrypto.so")]
        assert [f.build_id for f in report.frames[:2]] == ["aaaa", "bbbb"]

    def test_the_images_are_the_returned_frames_and_the_crash_sites(self):
        text = (FIXTURES / "android_dropbox" / "system_app_native_crash.dropbox").read_text()
        deep = "".join(f"      #{i:02d} pc {i:016x}  /system/lib64/libdeep{i}.so (f+4)\n"
                       for i in range(35))
        app = ("      #35 pc 0000000000001234  /data/app/~~x/com.example-1/lib/arm64/"
               "libapp.so (crash+8) (BuildId: abcd)\n")
        text = text.replace("backtrace:\n", "backtrace:\n" + deep + app, 1)
        report = self._native(text)
        assert len(report.frames) == crash_frames.MAX_FRAMES
        assert report.app_frame.image == "libapp.so"
        images = {i.name: i for i in report.images}
        assert images["libapp.so"].path.endswith("/lib/arm64/libapp.so")
        assert images["libapp.so"].uuid == "abcd"
        assert "libc.so" not in images          # only past the cap
        assert "libdeep34.so" not in images


class TestAndroidJava:
    def test_file_and_line(self):
        [frame] = java_frames(["at com.example.app.Foo.bar(Foo.java:42)"])
        assert (frame.symbol, frame.file, frame.line, frame.app) == (
            "com.example.app.Foo.bar", "Foo.java", 42, True)

    @pytest.mark.parametrize("where", ["Native Method", "Unknown Source"])
    def test_no_source(self, where):
        [frame] = java_frames([f"at com.example.app.Foo.bar({where})"])
        assert frame.file == "" and frame.line is None

    def test_unknown_source_keeps_its_line(self):
        """No file to show, but R8's line: what retrace maps the frame by. A
        minified release crash reads `at l82.onClick(Unknown Source:539)`."""
        [frame] = java_frames(["at l82.onClick(Unknown Source:539)"])
        assert frame.file == "" and frame.line == 539

    @pytest.mark.parametrize("symbol", [
        "android.app.ActivityThread.main", "java.lang.reflect.Method.invoke",
        "androidx.fragment.app.Fragment.performCreate", "kotlin.coroutines.Foo.bar",
        "com.android.internal.os.ZygoteInit.main",
    ])
    def test_the_platforms_frames_are_not_the_apps(self, symbol):
        [frame] = java_frames([f"at {symbol}(X.java:1)"])
        assert not frame.app

    def test_the_apps_package_decides(self):
        """Firebase, Gson, React Native: libraries the app ships, not its code."""
        lines = ["at com.google.gson.Gson.fromJson(Gson.java:1)",
                 "at com.example.app.Feed.parse(Feed.kt:12)",
                 "at a.b.c(Unknown Source:3)"]
        frames = java_frames(lines, package="com.example.app")
        assert [f.app for f in frames] == [False, True, False]

    @pytest.mark.parametrize("symbol", [
        "com.google.firebase.crashlytics.Foo.bar", "com.google.gson.Gson.fromJson",
        "com.facebook.react.bridge.X.y", "io.flutter.embedding.X.y",
        "com.google.common.base.Preconditions.check", "org.chromium.base.X.y",
    ])
    def test_common_libraries_are_not_the_app_without_a_package(self, symbol):
        [frame] = java_frames([f"at {symbol}(X.java:1)"])
        assert not frame.app

    def test_the_root_cause_is_where_it_began(self):
        """The outer exception is often only a wrapper rethrowing the cause."""
        from server.sources.android_dropbox import _java_trace

        body = (
            "java.lang.RuntimeException: Unable to start activity\n"
            "\tat android.app.ActivityThread.performLaunchActivity(ActivityThread.java:1)\n"
            "\tat com.example.app.Launcher.start(Launcher.kt:5)\n"
            "Caused by: java.lang.IllegalStateException: no token\n"
            "\tat com.example.app.Session.token(Session.kt:42)\n"
            "\tat com.example.app.Launcher.start(Launcher.kt:4)\n"
        )
        frames, app_frame, reason = _java_trace(body, "com.example.app")
        assert (app_frame.symbol, app_frame.line) == ("com.example.app.Session.token", 42)
        assert reason == "java.lang.IllegalStateException: no token"
        assert len(frames) == 4

    def test_the_package_line_names_the_app_and_its_versions(self):
        from server.sources.android_dropbox import device_zone, parse_dropbox

        text = (FIXTURES / "android_dropbox" / "system_app_crash.dropbox").read_text()
        report = parse_dropbox(text, serial="s", zone=device_zone("America/Los_Angeles", ""))[0]
        assert (report.bundle_id, report.app_version, report.build_version) == (
            "com.android.settings", "12", "32")

    def test_a_native_crashs_abort_message_is_its_reason(self):
        from server.sources.android_dropbox import device_zone, parse_dropbox

        text = (FIXTURES / "android_dropbox" / "system_app_native_crash.dropbox").read_text()
        text = text.replace(
            "\nbacktrace:", "\nAbort message: 'Check failed: mutex held'\nbacktrace:", 1)
        [report] = parse_dropbox(text, serial="s", zone=device_zone("America/Los_Angeles", ""))
        assert report.reason == "Check failed: mutex held"

    def test_an_anrs_main_thread_is_structured(self):
        """Both its `at` lines and its `native:` lines -- untested until now."""
        from server.sources.android_dropbox import device_zone, parse_dropbox

        text = (
            "========\n2026-09-27 10:00:00 data_app_anr (text, 1 bytes)\n"
            "Process: com.example.app\nPID: 4242\nPackage: com.example.app v7 (1.0)\n"
            "Subject: Input dispatching timed out\n\n"
            "----- pid 4242 at 2026-09-27 10:00:00 -----\n"
            '"main" prio=5 tid=1 Sleeping\n'
            "  native: #00 pc 000000000009e498  /apex/com.android.runtime/lib64/bionic/libc.so "
            "(__epoll_pwait+8) (BuildId: cd79)\n"
            "  at java.lang.Thread.sleep(Native method)\n"
            "  at com.example.app.Main.onClick(Main.java:42)\n\n"
            "----- end 4242 -----\n"
        )
        [report] = parse_dropbox(text, serial="s", zone=device_zone("America/Los_Angeles", ""))
        assert [(f.image or f.symbol) for f in report.frames] == [
            "libc.so", "java.lang.Thread.sleep", "com.example.app.Main.onClick"]
        assert (report.app_frame.symbol, report.app_frame.line) == (
            "com.example.app.Main.onClick", 42)

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


class TestWhichDevice:
    """A simulator's crash file names no device, so it was listed under every
    device's udid: an iPhone's crashes included the simulator's."""

    def test_a_simulator_report_names_its_simulator(self):
        report = _parse(FIXTURES / "crash_ips" / "simulator_debug.ips")
        assert report.device_id == "00000000-0000-0000-0000-00000000000A"

    def test_a_phone_report_is_left_to_the_pull_to_tag(self):
        report = _parse(FIXTURES / "crash_ips" / "device_debug.ips")
        assert report.device_id == ""

    def test_a_text_report_from_a_simulator(self, tmp_path):
        text = (FIXTURES / "crash_sample.crash").read_text().replace(
            "/private/var/containers/Bundle/Application/.../MyApp.app/MyApp",
            "/Users/USER/Library/Developer/CoreSimulator/Devices/"
            "45395d76-af20-4cef-8966-9b1c43bf9475/data/Containers/Bundle/Application/X/MyApp.app/MyApp")
        f = tmp_path / "MyApp.crash"
        f.write_text(text)
        assert _parse(f).device_id == "45395D76-AF20-4CEF-8966-9B1C43BF9475"


class TestWhoseCrashItIs:
    """#330: a simulator's system extension and the Mac's own processes were
    listed under every device's udid."""

    def test_a_simulators_system_extension_is_placed_by_its_coalition(self):
        """Real: Siri's widget extension runs from the runtime volume, so its
        path names no simulator; its coalition does."""
        report = _parse(FIXTURES / "crash_ips" / "simulator_system_extension.ips")
        assert report.device_id == "00000000-0000-0000-0000-00000000000B"
        assert report.mac_process is False

    def test_a_malformed_udid_in_the_path_is_not_a_device(self):
        path = "/x/CoreSimulator/Devices/------------------------------------/data/MyApp.app/MyApp"
        assert crash_frames.simulator_udid(path) == ""

    def test_the_apps_path_wins_over_the_coalition(self):
        assert crash_frames.simulator_udid(
            "/x/CoreSimulator/Devices/00000000-0000-0000-0000-00000000000A/data/MyApp.app/MyApp",
            "com.apple.CoreSimulator.SimDevice.00000000-0000-0000-0000-00000000000B",
        ) == "00000000-0000-0000-0000-00000000000A"

    @pytest.mark.parametrize("coalition", [
        "com.apple.CoreSimulator.SimDevice.not-a-udid", "com.example.agent",
        "com.apple.CoreSimulator.SimDevice.------------------------------------",
        "com.apple.CoreSimulator.SimDevice.00000000000000000000000000000000-000",
        "xcom.apple.CoreSimulator.SimDevice.00000000-0000-0000-0000-00000000000B",
    ])
    def test_only_a_simulator_coalition_places_it(self, coalition):
        assert crash_frames.simulator_udid("/usr/bin/x", coalition) == ""

    def test_a_mac_process_is_the_macs(self):
        """Real: a `node` crash on this Mac, names replaced."""
        report = _parse(FIXTURES / "crash_ips" / "mac_process.ips")
        assert report.mac_process is True and report.device_id == ""

    @pytest.mark.parametrize("name", ["simulator_debug", "simulator_fatal_error", "device_debug"])
    def test_a_simulators_or_a_phones_is_not(self, name):
        assert _parse(FIXTURES / "crash_ips" / f"{name}.ips").mac_process is False

    @pytest.mark.parametrize("marker", [
        {"parentProc": "launchd_sim"},
        {"procPath": "/Library/Developer/CoreSimulator/Volumes/iOS_23A/x/Siri.app/Siri"},
        {"coalitionName": "com.apple.CoreSimulator.SimDevice.unreadable"},
    ])
    def test_any_one_simulator_marker_is_enough(self, marker):
        header, body = _ips("mac_process")
        body.update(marker)
        assert crash_frames.is_mac_process(header, body) is False

    @pytest.mark.parametrize("platform", [7, 8, 9, 12])
    def test_a_simulator_platform_is_enough(self, platform):
        """Every real simulator report carries 7; a Mac process 0 or 1."""
        header, body = _ips("mac_process")
        header["platform"] = platform
        assert crash_frames.is_mac_process(header, body) is False

    def test_a_host_side_simulator_service_is_the_macs(self):
        """Only a simulated device's coalition says it ran in a simulator."""
        header, body = _ips("mac_process")
        body["coalitionName"] = "com.apple.CoreSimulator.CoreSimulatorService"
        assert crash_frames.is_mac_process(header, body) is True

    def test_the_headers_os_wins_over_the_bodys(self):
        header, body = _ips("mac_process")
        header["os_version"] = "iPhone OS 26.5.2 (23F84)"
        assert crash_frames.is_mac_process(header, body) is False

    @pytest.mark.parametrize("os_version", [
        "iPhone OS 26.5.2 (23F84)", "iPadOS 26.0 (23A1)", "xrOS 26.0", "watchOS 26.0", "",
    ])
    def test_only_macos_is_claimed(self, os_version):
        header, body = _ips("mac_process")
        header["os_version"] = os_version
        body["osVersion"] = {"train": os_version}
        assert crash_frames.is_mac_process(header, body) is False

    def test_the_os_from_the_body_when_the_header_has_none(self):
        header, body = _ips("mac_process")
        header.pop("os_version")
        assert crash_frames.is_mac_process(header, body) is True
        body.pop("osVersion")
        assert crash_frames.is_mac_process(header, body) is False   # cannot tell: not claimed


class TestWhoEndedIt:
    """Second review, with crash reports it produced on purpose: `byPid ==
    pid` marks a self-inflicted crash in every one of them, and the name does
    not -- the kernel cuts it at 32 characters."""

    def test_an_abort_from_a_long_named_process_is_its_own(self, tmp_path):
        """Measured: abort() in `…_well_past_thirty_two_chars` carried byProc
        `…_well_p` and read as a kill, hiding the crash site."""
        header, body = _ips("simulator_fatal_error")
        body["procName"] = "qrprobe_abort_with_a_name_well_past_thirty_two_chars"
        body["termination"].update(byProc="qrprobe_abort_with_a_name_well_p", byPid=body["pid"])
        report = _parse(_write(tmp_path, header, body))
        assert report.killed_by == "" and report.app_frame is not None

    def test_a_kill_from_another_process_of_the_same_name(self, tmp_path):
        """A second copy of the app, or a helper sharing its executable's name:
        the name says it was its own doing, the pid says otherwise."""
        header, body = _ips("simulator_fatal_error")
        body["termination"].update(byProc=body["procName"], byPid=body["pid"] + 1)
        assert _parse(_write(tmp_path, header, body)).killed_by == body["procName"]

    def test_an_uncaught_exception_carries_the_apps_own_name(self, tmp_path):
        """What a real one reads; the earlier test used `exc handler`."""
        header, body = _ips("simulator_fatal_error")
        body["termination"].update(byProc="MyApp", byPid=body["pid"])
        assert _parse(_write(tmp_path, header, body)).killed_by == ""

    def test_by_name_when_the_pids_are_missing(self, tmp_path):
        header, body = _ips("simulator_fatal_error")
        body.pop("pid")
        body["termination"].pop("byPid")
        body["termination"]["byProc"] = "MyApp"
        assert _parse(_write(tmp_path, header, body)).killed_by == ""
        body["termination"]["byProc"] = "zsh"
        assert _parse(_write(tmp_path, header, body)).killed_by == "zsh"

    @pytest.mark.parametrize("by_proc", ["exc handler", "qrprobe_abort_with_a_name_well_p"])
    def test_by_name_the_handler_and_a_cut_name_are_its_own(self, tmp_path, by_proc):
        """Without pids: the exception handler, and the app's own name as the
        kernel cuts it at 32 characters, are the app ending itself."""
        header, body = _ips("simulator_fatal_error")
        body.pop("pid")
        body["procName"] = "qrprobe_abort_with_a_name_well_past_thirty_two_chars"
        body["termination"].pop("byPid")
        body["termination"]["byProc"] = by_proc
        assert _parse(_write(tmp_path, header, body)).killed_by == ""

    def test_a_watchdog_keeps_where_it_hung(self, tmp_path):
        """The system ended it, but the main thread's frames are the answer."""
        header, body = _ips("simulator_fatal_error")
        body["termination"] = {"namespace": "FRONTBOARD", "code": 2343432205,
                               "byProc": "runningboardd", "byPid": 55,
                               "reasons": ["scene-update watchdog transgression: exhausted "
                                           "real (wall clock) time allowance of 10.00 seconds"]}
        report = _parse(_write(tmp_path, header, body))
        assert report.killed_by == ""
        assert report.app_frame.symbol == "closure #1 in SettingsPresenter.resetStore()"
        assert "watchdog transgression" in report.reason


class TestReviewTwoShapes:
    def test_the_crash_site_past_the_frame_cap_is_still_found(self, tmp_path):
        """A deep UIKit or SwiftUI stack put the app's frame past the cap."""
        header, body = _self_crash("simulator_fatal_error")
        frames = body["threads"][0]["frames"]
        system = frames[0]["imageIndex"]
        app_frame = frames[1]
        filler = [{"imageIndex": system, "imageOffset": 4 * i, "symbol": f"AG::Graph::f{i}"}
                  for i in range(40)]
        body["threads"][0]["frames"] = filler + [app_frame]
        report = _parse(_write(tmp_path, header, body))
        assert report.app_frame.file == "SettingsPresenter.swift"
        assert len(report.frames) == crash_frames.MAX_FRAMES
        assert "MyApp.debug.dylib" in {i.name for i in report.images}   # its image kept

    def test_a_real_path_outside_the_bundle_is_not_the_app_by_name(self, tmp_path):
        """An app named Contacts would otherwise claim Apple's Contacts framework."""
        header, body = _self_crash("device_debug")
        first = body["usedImages"][body["threads"][0]["frames"][0]["imageIndex"]]
        first.update(name="MyApp", path="/System/Library/Frameworks/MyApp.framework/MyApp")
        assert not _parse(_write(tmp_path, header, body)).frames[0].app

    @pytest.mark.parametrize("mangle", [
        lambda b: b.update(exception="boom"),
        lambda b: b.update(termination="gone", exception={}),
        lambda b: b.update(procName=42),
        lambda b: b.update(captureTime=12345),
    ])
    def test_odd_top_level_fields_do_not_drop_the_report(self, tmp_path, mangle):
        header, body = _ips("device_debug")
        mangle(body)
        assert _parse(_write(tmp_path, header, body)) is not None


class TestReviewTwoAndroid:
    def _record(self, package, body):
        return ("========\n2026-09-27 10:00:00 data_app_crash (text, 1 bytes)\n"
                f"Process: com.example.app\nPID: 7\nPackage: {package}\n\n{body}")

    def _parse(self, text):
        from server.sources.android_dropbox import device_zone, parse_dropbox

        return parse_dropbox(text, serial="s", zone=device_zone("America/Los_Angeles", ""))[0]

    @pytest.mark.parametrize("package", [
        "com.example.app.debug v7 (1.0)", "com.example.app.staging v7 (1.0)",
        "com.example.app v7 (1.0)",
    ])
    def test_a_debug_builds_suffix_still_finds_the_apps_code(self, package):
        """applicationIdSuffix is the norm for the Debug builds quern debugs;
        matching the whole id found no app frame at all."""
        report = self._parse(self._record(package, (
            "java.lang.IllegalStateException: boom\n"
            "\tat androidx.lifecycle.X.y(X.java:1)\n"
            "\tat com.example.app.Feed.parse(Feed.kt:12)\n")))
        assert report.app_frame.symbol == "com.example.app.Feed.parse"

    def test_a_module_under_the_apps_namespace(self):
        report = self._parse(self._record("com.acme.app v7 (1.0)", (
            "java.lang.IllegalStateException: boom\n"
            "\tat com.acme.feature.Map.draw(Map.kt:3)\n")))
        assert report.app_frame.symbol == "com.acme.feature.Map.draw"

    def test_a_shared_top_level_domain_is_not_the_apps_package(self):
        """`com.` is everyone's: with nothing under `com.acme` the package
        decides nothing, and every non-library frame counts."""
        report = self._parse(self._record("com.acme.app v7 (1.0)", (
            "java.lang.IllegalStateException: boom\n"
            "\tat org.thirdparty.Z.run(Z.kt:1)\n"
            "\tat com.vendor.sdk.Lib.go(Lib.kt:2)\n")))
        assert [f.app for f in report.frames] == [True, True]

    def test_every_block_agrees_on_the_apps_package(self):
        """The trace names the app's package once: a cause whose frames match
        only a shorter prefix is not the app's because its own block lacks a
        full match."""
        report = self._parse(self._record("com.acme.app v7 (1.0)", (
            "java.lang.RuntimeException: wrapped\n"
            "\tat com.acme.app.Main.run(Main.kt:1)\n"
            "Caused by: java.lang.IllegalStateException: boom\n"
            "\tat com.acme.lib.Parser.read(Parser.kt:2)\n")))
        assert [f.app for f in report.frames] == [True, False]
        assert report.app_frame.symbol == "com.acme.app.Main.run"

    def test_the_crash_site_past_the_frame_cap(self):
        compose = "".join(f"\tat androidx.compose.ui.N{i}.f(N.kt:1)\n" for i in range(35))
        report = self._parse(self._record("com.example.app v7 (1.0)", (
            "java.lang.IllegalStateException: boom\n" + compose
            + "\tat com.example.app.Screen.render(Screen.kt:9)\n")))
        assert report.app_frame.symbol == "com.example.app.Screen.render"
        assert len(report.frames) == crash_frames.MAX_FRAMES

    def test_a_single_exception_is_its_own_root_cause(self):
        report = self._parse(self._record("com.example.app v7 (1.0)", (
            "java.lang.IllegalStateException: boom\n"
            "\tat com.example.app.Feed.parse(Feed.kt:12)\n")))
        assert report.reason == "java.lang.IllegalStateException: boom"
        assert report.frames_from == "exception"

    def test_what_the_frames_are_per_kind(self):
        from server.sources.android_dropbox import device_zone, parse_dropbox

        zone = device_zone("America/Los_Angeles", "")
        for name, expected in (("system_app_crash", "exception"),
                               ("system_app_native_crash", "crashing_thread")):
            text = (FIXTURES / "android_dropbox" / f"{name}.dropbox").read_text()
            assert parse_dropbox(text, serial="s", zone=zone)[0].frames_from == expected

    def test_an_anr_uses_the_package_and_is_the_main_thread(self):
        from server.sources.android_dropbox import device_zone, parse_dropbox

        text = (
            "========\n2026-09-27 10:00:00 data_app_anr (text, 1 bytes)\n"
            "Process: com.example.app\nPID: 4242\nPackage: com.example.app.debug v7 (1.0)\n"
            "Subject: Input dispatching timed out\n\n"
            "----- pid 4242 at 2026-09-27 10:00:00 -----\n"
            '"main" prio=5 tid=1 Sleeping\n'
            "  at a.b.c(Unknown Source:3)\n"
            "  at com.example.app.Main.onClick(Main.java:42)\n\n"
            "----- end 4242 -----\n"
        )
        [report] = parse_dropbox(text, serial="s", zone=device_zone("America/Los_Angeles", ""))
        assert report.app_frame.symbol == "com.example.app.Main.onClick"   # not obfuscated a.b.c
        assert report.frames_from == "main_thread"
