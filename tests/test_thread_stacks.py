"""get_thread_stacks: what an app's threads are doing right now (#245, phase 5).

The fixtures are real: `sample` on the probe in an iOS 26 simulator, and a
stackshot of it on an iPhone 11 (iOS 26.7), both at rest -- the main thread
waiting in its run loop.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.device import performance
from server.device import thread_stacks as ts
from server.models import DeviceError, InvalidDeviceRequestError
from server.sources import symbolicate

FIXTURES = Path(__file__).parent / "fixtures" / "stacks"
SIM = "F5AF3736-C05F-493F-AA52-CA883B13B18C"
PHONE = "00008030-000C59623A69802E"
APP = "com.quern.probe"
DEVICE = ("iPhone12,1", "26.7", "23H24")
#: The fixture's shared cache: its slide, and so where its images are.
SLIDE = 215121920
SLID_BASE = 6657572864


def sample_text() -> str:
    return (FIXTURES / "sim-sample-idle.txt").read_text()


def stackshot() -> dict:
    return json.loads((FIXTURES / "device-stackshot-idle.json").read_text())


# -- sample ------------------------------------------------------------------

class TestParseSample:
    def test_the_main_thread_at_rest(self):
        threads = ts.parse_sample(sample_text(), app_image="QuernProbe")
        main = threads[0]
        assert main.main and main.queue == ts.MAIN_QUEUE and main.id == 178997863
        # Innermost first: waiting for a message, called from the run loop,
        # entered from dyld's start.
        assert [f.symbol for f in main.frames[:3]] == [
            "mach_msg2_trap", "mach_msg2_internal", "mach_msg_overwrite"]
        assert main.frames[0].image == "libsystem_kernel.dylib"
        assert main.frames[0].symbol_offset == 8
        assert main.frames[-1].symbol == "start" and main.frames[-1].image == "dyld"
        assert (main.samples_on_stack, main.samples) == (804, 804)

    def test_a_hung_main_thread(self):
        """The probe's 12 s `Thread.sleep` on its main thread, sampled live:
        under a tap's event handling. 727 of its 746 samples are on that
        path; the rest fell around the sleep's start or end."""
        text = (FIXTURES / "sim-sample-hang.txt").read_text()
        main = ts.parse_sample(text, app_image="QuernProbe")[0]
        assert [f.symbol for f in main.frames[:5]] == [
            "__semwait_signal", "nanosleep", "+[NSThread sleepForTimeInterval:]",
            "DiagViewController.hang()", "@objc DiagViewController.hang()"]
        assert [f.app for f in main.frames[:5]] == [False, False, False, True, True]
        assert (main.samples_on_stack, main.samples) == (727, 746)
        assert main.frames[-1].symbol == "start"

    def test_the_apps_own_frames_are_marked(self):
        main = ts.parse_sample(sample_text(), app_image="QuernProbe")[0]
        assert [f.symbol for f in main.frames if f.app] == [
            "static UIApplicationDelegate.main()", "static AppDelegate.$main()", "main"]

    def test_a_frame_with_no_symbol_keeps_its_offset_into_the_image(self):
        main = ts.parse_sample(sample_text())[0]
        [unnamed] = [f for f in main.frames if not f.symbol]
        assert (unnamed.image, unnamed.offset) == ("UIKitCore", 0x228F88)

    def test_named_threads_and_queues(self):
        threads = ts.parse_sample(sample_text())
        assert any(t.name == "com.apple.uikit.eventfetch-thread" for t in threads)
        assert sum(t.main for t in threads) == 1

    def test_a_busy_thread_follows_its_heaviest_path(self):
        """Split work: the path it was on most, and how often, so a busy
        thread does not read as a stuck one."""
        text = """Call graph:
    1000 Thread_7   DispatchQueue_1: com.apple.main-thread  (serial)
    + 1000 start  (in dyld) + 1  [0x1]
    +   700 work()  (in App) + 10  [0x2]  Work.swift:12
    +   ! 700 crunch()  (in App) + 4  [0x3]
    +   300 draw()  (in App) + 8  [0x4]

Total number in stack (recursive counted multiple times):
"""
        [t] = ts.parse_sample(text, app_image="App")
        assert [f.symbol for f in t.frames] == ["crunch()", "work()", "start"]
        assert (t.frames[1].file, t.frames[1].line) == ("Work.swift", 12)
        assert (t.samples_on_stack, t.samples) == (700, 1000)
        assert t.frames[0].app and not t.frames[-1].app

    def test_no_call_graph_is_no_threads(self):
        assert ts.parse_sample("Sampling process 1 for 1 second\n") == []

    def test_without_a_main_queue_the_first_thread_is_main(self):
        text = "Call graph:\n    5 Thread_1\n    + 5 f  (in A) + 1  [0x1]\n    5 Thread_2\n"
        threads = ts.parse_sample(text)
        assert [t.main for t in threads] == [True, False]


class FakeSimctl:
    def __init__(self, pids):
        self.pids = list(pids)

    async def running_pid(self, udid, bundle_id):
        return self.pids.pop(0) if len(self.pids) > 1 else self.pids[0]


def fake_sample(monkeypatch, text: str | None, rc=0, err=""):
    calls = []

    async def run(*args, timeout):
        calls.append(args)
        if text is not None:
            Path(args[args.index("-file") + 1]).write_text(text)
        return rc, "", err
    monkeypatch.setattr(performance, "_run", run)
    return calls


class TestSimulator:
    async def test_the_main_thread(self, monkeypatch):
        calls = fake_sample(monkeypatch, sample_text())
        out = await ts.simulator_stacks(FakeSimctl([49423]), SIM, APP,
                                        all_threads=False, max_frames=40)
        assert calls[0][:3] == ("sample", "49423", "1")
        assert (out.running, out.pid, out.source) == (True, 49423, "sample")
        assert len(out.threads) == 1 and out.threads[0].main
        assert out.thread_count == 5 and not out.unavailable
        assert out.threads[0].frames[3].app is False          # libsystem, not the app

    async def test_all_threads_main_first_and_cut(self, monkeypatch):
        fake_sample(monkeypatch, sample_text())
        out = await ts.simulator_stacks(FakeSimctl([49423]), SIM, APP,
                                        all_threads=True, max_frames=3)
        assert len(out.threads) == 5 and out.threads[0].main
        main = out.threads[0]
        assert len(main.frames) == 3 and main.frames_cut == 13
        assert main.frames[0].symbol == "mach_msg2_trap"      # innermost kept

    async def test_not_running_runs_nothing(self, monkeypatch):
        calls = fake_sample(monkeypatch, sample_text())
        out = await ts.simulator_stacks(FakeSimctl([None]), SIM, APP,
                                        all_threads=False, max_frames=40)
        assert out.running is False and out.threads is None and not calls
        assert out.unavailable["threads"] == performance.NOT_RUNNING

    async def test_an_app_that_exits_while_sampled_is_not_running(self, monkeypatch):
        fake_sample(monkeypatch, None, rc=1, err="sample cannot examine process 49423")
        out = await ts.simulator_stacks(FakeSimctl([49423, None]), SIM, APP,
                                        all_threads=False, max_frames=40)
        assert out.running is False and out.threads is None

    async def test_sample_failing_on_a_running_app_is_an_error(self, monkeypatch):
        """Not "not running": the app is still there, and sample could not read it."""
        fake_sample(monkeypatch, None, rc=1, err="sample: permission denied")
        with pytest.raises(DeviceError, match="permission denied"):
            await ts.simulator_stacks(FakeSimctl([49423, 49423]), SIM, APP,
                                      all_threads=False, max_frames=40)

    async def test_a_report_with_no_call_graph_is_an_error(self, monkeypatch):
        fake_sample(monkeypatch, "Analysis of sampling QuernProbe (pid 49423)\n")
        with pytest.raises(DeviceError, match="no call graph"):
            await ts.simulator_stacks(FakeSimctl([49423]), SIM, APP,
                                      all_threads=False, max_frames=40)


# -- a stackshot ---------------------------------------------------------------

def macho(vmaddr: int, vmsize: int, fat: bool = False) -> bytes:
    """A thin arm64 Mach-O with one LC_SEGMENT_64 __TEXT, or that inside a fat
    file behind an x86_64 slice."""
    seg = struct.pack("<II16sQQQQiiII", 0x19, 72, b"__TEXT", vmaddr, vmsize, 0, vmsize,
                      5, 5, 0, 0)
    thin = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 2, 6, 1, len(seg), 0, 0) + seg
    if not fat:
        return thin
    header = struct.pack(">II", 0xCAFEBABE, 2)
    header += struct.pack(">iiIII", 0x01000007, 3, 4096, len(thin), 12)
    header += struct.pack(">iiIII", 0x0100000C, 2, 8192, len(thin), 14)
    return header.ljust(8192, b"\0") + thin


@pytest.fixture
def device_support(tmp_path, monkeypatch):
    """A DeviceSupport folder holding the two OS images the fixture's main
    thread passes through, at their unslid cache addresses."""
    ts._INDEX.clear()
    root = tmp_path / "iOS DeviceSupport"
    lib = root / "iPhone12,1 26.7 (23H24)" / "Symbols" / "usr" / "lib" / "system"
    lib.mkdir(parents=True)
    (lib / "libsystem_kernel.dylib").write_bytes(macho(0x2_32B4_0000, 0x40000))
    fw = root / "iPhone12,1 26.7 (23H24)" / "Symbols" / "System" / "Library" / "Frameworks"
    fw.mkdir(parents=True)
    (fw / "CoreFoundation").write_bytes(macho(0x1_8350_0000, 0x400000, fat=True))
    (fw / "NotMachO").write_bytes(b"hello")
    monkeypatch.setattr(ts, "DEVICE_SUPPORT", root)
    yield root
    ts._INDEX.clear()


class FakeFinder(symbolicate.SymbolFinder):
    """A SymbolFinder that never asks Spotlight or reads records: `found`
    maps UUIDs to a dSYM, and atos's answers come from `atos`."""

    def __init__(self, found=None, atos=None):
        super().__init__(records_root=Path("/nonexistent"), run=self._fake_run)
        self.found = found or {}
        self.atos = atos or (lambda argv: (0, "", ""))
        self.calls: list[list[str]] = []

    async def find(self, uuid, read=None):
        hit = self.found.get(symbolicate.normalise_uuid(uuid))
        return symbolicate.Lookup(symbolicate.Found(Path(hit), "spotlight") if hit else None,
                                  "" if hit else "no dSYM for it")

    async def _fake_run(self, argv):
        self.calls.append(argv)
        return self.atos(argv)


def atos_names(argv):
    """atos answering each address as `fn_<hex> (in <image>) + 4`."""
    image = Path(argv[argv.index("-o") + 1]).name
    addresses = [a for a in argv[argv.index("-l") + 2:]]
    return 0, "\n".join(f"fn_{a[2:]} (in {image}) + 4" for a in addresses) + "\n", ""


class TestReadShot:
    def test_the_apps_threads_images_and_cache(self):
        shot = ts.read_shot(stackshot(), 2745, DEVICE)
        assert [t[0] for t in shot.threads] == [1511172, 1511183, 1511193]
        assert shot.threads[0][2] == ts.MAIN_QUEUE and len(shot.threads[0][3]) == 14
        assert shot.threads[2][1] == "com.apple.uikit.eventfetch-thread"
        assert [u for _, u in shot.images] == ["99a58e82-7b7f-367f-8033-98296c641d77",
                                              "4e35b543-655f-3a1a-b4b1-3e9e096a5117"]
        assert (shot.slide, shot.cache_base) == (SLIDE, SLID_BASE)

    def test_the_trailing_zero_frame_is_dropped(self):
        """The fixture's main thread ends in a frame of lr 0: not a frame."""
        raw = stackshot()["task_snapshots"]["2745"]["thread_snapshots"]["1511172"]
        assert raw["user_stack_frames"][-1] == {"lr": 0}
        assert 0 not in ts.read_shot(stackshot(), 2745, DEVICE).threads[0][3]

    def test_an_app_not_in_it(self):
        assert ts.read_shot(stackshot(), 1, DEVICE) is None


class TestIndex:
    def test_thin_and_fat_images_at_cache_addresses(self, device_support):
        symbols = ts.device_support_dir(*DEVICE)
        index = ts.build_index(symbols)
        assert [(Path(p).name, start) for start, _, p in index] == [
            ("CoreFoundation", 0x1_8350_0000), ("libsystem_kernel.dylib", 0x2_32B4_0000)]

    def test_an_image_below_the_shared_region_is_left_out(self, tmp_path):
        (tmp_path / "daemon").write_bytes(macho(0x1_0000_0000, 0x4000))
        assert ts.build_index(tmp_path) == []

    def test_the_older_folder_name(self, tmp_path):
        (tmp_path / "26.7 (23H24)" / "Symbols").mkdir(parents=True)
        assert ts.device_support_dir(*DEVICE, root=tmp_path).parent.name == "26.7 (23H24)"
        assert ts.device_support_dir("iPhone12,1", "26.8", "23I1", root=tmp_path) is None

    @pytest.mark.parametrize("data", [b"", b"\xcf\xfa\xed\xfe" + b"\0" * 12 + b"\xff\xff",
                                      b"\xca\xfe\xba\xbe\x00\x00\x00\x01" + b"\0" * 20])
    def test_a_broken_file_is_no_range(self, tmp_path, data):
        (tmp_path / "f").write_bytes(data)
        assert ts._text_range(str(tmp_path / "f")) is None


class TestSymbolicateShot:
    def _shot(self, **kw):
        shot = ts.read_shot(stackshot(), 2745, DEVICE)
        for k, v in kw.items():
            setattr(shot, k, v)
        return shot

    async def test_os_frames_from_device_support(self, device_support):
        finder = FakeFinder(atos=atos_names)
        threads, symbols = await ts.symbolicate_shot(self._shot(), finder, APP)
        main = threads[0]
        top = main.frames[0]
        # 0x23f89ccd4 - slide = 0x232b74cd4: in libsystem_kernel, looked up
        # as itself -- the innermost frame is not a return address.
        assert (top.image, top.offset) == ("libsystem_kernel.dylib", 0x34CD4)
        assert top.symbol == "fn_232b74cd4" and top.symbol_offset == 4
        # The next is a return address: asked a byte early, and its offset
        # into the symbol given back as the phone's.
        second = main.frames[1]
        assert second.symbol == "fn_232b7822b" and second.symbol_offset == 5
        cf = next(s for s in symbols if s.image == "CoreFoundation")
        assert cf.source == "device_support" and cf.frames_resolved == cf.frames_total == 3
        kernel_call = next(c for c in finder.calls if "libsystem_kernel.dylib" in c[3])
        assert kernel_call[4:6] == ["-l", hex(0x2_32B4_0000)]

    async def test_without_device_support_the_os_frames_say_why(self, tmp_path, monkeypatch):
        ts._INDEX.clear()
        monkeypatch.setattr(ts, "DEVICE_SUPPORT", tmp_path)
        finder = FakeFinder(atos=atos_names)
        threads, symbols = await ts.symbolicate_shot(self._shot(), finder, APP)
        top = threads[0].frames[0]
        assert top.image == "dyld_shared_cache" and not top.symbol
        assert top.offset == 0x23F89CCD4 - SLID_BASE
        [note] = [s for s in symbols if s.image == "dyld_shared_cache"]
        assert "iPhone12,1 26.7 (23H24)" in note.note and "Xcode" in note.note
        assert note.uuid == "a0c64eb3-49c0-387e-8df7-d752639bac09"
        assert not any("libsystem" in " ".join(c) for c in finder.calls)

    async def test_the_apps_frames_through_the_crash_symbolicator(self, device_support,
                                                                  tmp_path):
        dwarf = tmp_path / "QuernProbe.app.dSYM" / "Contents" / "Resources" / "DWARF"
        dwarf.mkdir(parents=True)
        (dwarf / "QuernProbe").write_bytes(b"x")
        finder = FakeFinder(
            found={"99A58E82-7B7F-367F-8033-98296C641D77": str(dwarf / "QuernProbe")},
            atos=lambda argv: (0, "".join(f"AppDelegate.main() (in QuernProbe) (App.swift:{i})\n"
                                          for i, _ in enumerate(argv[argv.index("-l") + 2:], 1)),
                               "") if "QuernProbe" in argv[3] else atos_names(argv))
        threads, symbols = await ts.symbolicate_shot(self._shot(), finder, APP)
        app = [f for f in threads[0].frames if f.app]
        assert [f.image for f in app] == ["QuernProbe"] * 3
        assert all(f.symbol == "AppDelegate.main()" and f.file == "App.swift" for f in app)
        entry = next(s for s in symbols if s.image == "QuernProbe")
        assert entry.frames_resolved == 3 and entry.source == "spotlight"

    async def test_an_app_image_with_no_dsym_says_so(self, device_support):
        threads, symbols = await ts.symbolicate_shot(
            self._shot(), FakeFinder(atos=atos_names), APP)
        app = [f for f in threads[0].frames if f.app]
        assert app and all(not f.symbol for f in app)
        assert app[0].image == f"{APP} image 99a58e82" and app[0].offset == 0x20264
        entry = next(s for s in symbols if s.image == app[0].image)
        assert entry.frames_resolved == 0 and "no dSYM" in entry.note

    async def test_a_stripped_function_is_no_name(self, device_support):
        """atos names what the OS stripped by its own address: measured in
        UIKitCore. That is not a symbol."""
        def atos(argv):
            n = len(argv) - 6
            return 0, "0x00000001893e8aa3 (in CoreFoundation) + 104\n" * n, ""
        threads, symbols = await ts.symbolicate_shot(self._shot(), FakeFinder(atos=atos), APP)
        cf = [f for f in threads[0].frames if f.image == "CoreFoundation"]
        assert cf and all(not f.symbol for f in cf)
        entry = next(s for s in symbols if s.image == "CoreFoundation")
        assert entry.frames_resolved == 0 and "strips" in entry.note

    async def test_atos_failing_leaves_the_frames_unnamed(self, device_support):
        finder = FakeFinder(atos=lambda argv: (1, "", "atos: cannot load"))
        threads, symbols = await ts.symbolicate_shot(self._shot(), finder, APP)
        assert not threads[0].frames[0].symbol
        assert "atos exited 1" in next(s for s in symbols if s.image.startswith("libsystem")).note

    async def test_a_short_atos_answer_is_not_matched_up(self, device_support):
        """One line per address or none: a misaligned answer names wrong frames."""
        finder = FakeFinder(atos=lambda argv: (0, "only (in x) + 1\n", ""))
        threads, symbols = await ts.symbolicate_shot(self._shot(), finder, APP)
        cf = next(s for s in symbols if s.image == "CoreFoundation")
        assert "lines for 3 addresses" in cf.note and cf.frames_resolved == 0


class FakeTap:
    error: Exception | None = None

    def __init__(self, dvt, config):
        pass

    @staticmethod
    async def get_time_config(dvt):
        return {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_stackshot(self, timeout):
        if FakeTap.error:
            raise FakeTap.error
        return stackshot()


class FakeRsd:
    def __init__(self, address):
        self.peer_info = {"Properties": {"ProductType": "iPhone12,1", "OSVersion": "26.7",
                                         "BuildVersion": "23H24"}}
        self.closed = False

    async def connect(self):
        pass

    async def close(self):
        self.closed = True


class FakeService:
    def __init__(self, *_, pid=2745):
        self.pid = pid

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def process_identifier_for_bundle_identifier(self, bundle_id):
        return self.pid


@pytest.fixture
def phone(monkeypatch):
    knobs = SimpleNamespace(pid=2745)
    FakeTap.error = None
    monkeypatch.setattr(performance, "_device_address", AsyncMock(return_value=("fd::1", 1)))
    monkeypatch.setattr(ts, "_pmd3", lambda: SimpleNamespace(
        RemoteServiceDiscoveryService=FakeRsd, DvtProvider=FakeService,
        ProcessControl=lambda dvt: FakeService(pid=knobs.pid),
        CoreProfileSessionTap=FakeTap))
    return knobs


class TestDevice:
    async def test_the_main_thread_symbolicated(self, phone, device_support):
        out = await ts.device_stacks(PHONE, APP, FakeFinder(atos=atos_names),
                                     all_threads=False, max_frames=40)
        assert (out.running, out.pid, out.source) == (True, 2745, "stackshot")
        assert out.thread_count == 3 and len(out.threads) == 1
        main = out.threads[0]
        assert main.main and main.queue == ts.MAIN_QUEUE and main.frames_cut == 0
        assert main.frames[0].image == "libsystem_kernel.dylib"
        assert {s.image for s in out.symbols} >= {"libsystem_kernel.dylib", "CoreFoundation"}

    async def test_only_the_threads_shown_are_symbolicated(self, phone, device_support):
        def asked(all_threads):
            finder = FakeFinder(atos=atos_names)
            return finder, ts.device_stacks(PHONE, APP, finder, all_threads=all_threads,
                                            max_frames=40)

        main_only, run = asked(False)
        await run
        everything, run = asked(True)
        await run
        # Addresses handed to atos: the main thread's six OS frames in the
        # fixture's two images, not the event fetch thread's as well.
        assert sum(len(c) - 6 for c in main_only.calls) == 6
        assert sum(len(c) - 6 for c in everything.calls) > 6

    async def test_all_threads_and_a_cut(self, phone, device_support):
        out = await ts.device_stacks(PHONE, APP, FakeFinder(atos=atos_names),
                                     all_threads=True, max_frames=2)
        assert [t.main for t in out.threads] == [True, False, False]
        assert [len(t.frames) for t in out.threads] == [2, 2, 2]
        assert out.threads[0].frames_cut == 12
        assert out.threads[0].frames[0].image == "libsystem_kernel.dylib"   # innermost kept

    async def test_not_running_takes_no_stackshot(self, phone):
        phone.pid = 0
        FakeTap.error = AssertionError("taken")
        out = await ts.device_stacks(PHONE, APP, FakeFinder(), all_threads=False,
                                     max_frames=40)
        assert out.running is False and out.threads is None
        assert out.unavailable["threads"] == performance.NOT_RUNNING

    async def test_an_app_gone_from_the_shot(self, phone, device_support):
        phone.pid = 9999
        out = await ts.device_stacks(PHONE, APP, FakeFinder(), all_threads=False,
                                     max_frames=40)
        assert out.running and out.threads is None and "9999" in out.unavailable["threads"]

    async def test_the_kernel_profiler_in_use_is_named(self, phone):
        FakeTap.error = RuntimeError("kperf is already owned by another session")
        with pytest.raises(DeviceError, match="kernel profiler is in use"):
            await ts.device_stacks(PHONE, APP, FakeFinder(), all_threads=False,
                                   max_frames=40)

    async def test_any_other_failure_is_a_device_error(self, phone):
        FakeTap.error = ValueError("bad payload")
        with pytest.raises(DeviceError, match="ValueError: bad payload"):
            await ts.device_stacks(PHONE, APP, FakeFinder(), all_threads=False,
                                   max_frames=40)


# -- the controller ------------------------------------------------------------

class TestController:
    def _controller(self, kind):
        from server.device.controller import DeviceController

        c = DeviceController.__new__(DeviceController)
        c.resolve_udid = AsyncMock(return_value="X")
        c._is_android = lambda u: kind == "android"
        c._is_physical = lambda u: kind == "phone"
        c._device_type = lambda u: kind
        return c

    async def test_android_says_it_needs_root(self):
        out = await self._controller("android").get_thread_stacks(APP, FakeFinder())
        assert out.platform == "android" and out.running is None and out.threads is None
        assert "root" in out.unavailable["threads"]

    async def test_a_bad_bundle_id_is_refused_before_any_device(self):
        c = self._controller("android")
        with pytest.raises(InvalidDeviceRequestError):
            await c.get_thread_stacks("com.app; reboot", FakeFinder())
        c.resolve_udid.assert_not_awaited()
