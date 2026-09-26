"""Android crashes become crash entries, and reach the crash buffer (#255).

An Android crash has no report file on the host; it is a few logcat lines at
the same level as everything else. Until this existed it was an ordinary
logcat line -- evictable by the firehose in seconds, and never in the crash
buffer. The Java lines below are copied from an API 32 emulator after
`adb shell am crash com.android.settings`.
"""

from __future__ import annotations

from server.models import LogLevel, LogSource
from server.sources.android_crash import AndroidCrashDetector
from server.sources.logcat import LogcatAdapter

JAVA_CRASH = [
    "2026-09-26 18:05:07.696 +0000 25084 25084 E AndroidRuntime: FATAL EXCEPTION: main",
    "2026-09-26 18:05:07.696 +0000 25084 25084 E AndroidRuntime: "
    "Process: com.android.settings, PID: 25084",
    "2026-09-26 18:05:07.696 +0000 25084 25084 E AndroidRuntime: "
    "android.app.RemoteServiceException$CrashedByAdbException: shell-induced crash",
    "2026-09-26 18:05:07.696 +0000 25084 25084 E AndroidRuntime: "
    "\tat android.app.ActivityThread.throwRemoteServiceException(ActivityThread.java:1)",
]


def _entries(lines):
    adapter = LogcatAdapter(serial="emulator-5554", device_id="emulator-5554")
    return [adapter._parse_line(line) for line in lines]


def _feed(lines, detector=None):
    detector = detector or AndroidCrashDetector(device_id="emulator-5554")
    out = []
    for entry in _entries(lines):
        out.extend(detector.feed(entry))
    return out, detector


class TestJavaCrash:
    def test_the_real_crash_becomes_one_crash_entry(self):
        crashes, _ = _feed(JAVA_CRASH)

        [crash] = crashes
        assert crash.source == LogSource.CRASH
        assert crash.level == LogLevel.FAULT
        assert crash.process == "com.android.settings"
        assert crash.pid == 25084
        assert crash.device_id == "emulator-5554"
        assert "CrashedByAdbException: shell-induced crash" in crash.message
        assert "FATAL EXCEPTION: main" in crash.raw

    def test_it_is_stamped_when_the_crash_began(self):
        [crash], _ = _feed(JAVA_CRASH)
        assert crash.timestamp.isoformat() == "2026-09-26T18:05:07.696000+00:00"

    def test_interleaved_crashes_are_kept_apart(self):
        other = [line.replace("25084", "31000").replace("com.android.settings", "com.example")
                 for line in JAVA_CRASH[:3]]
        interleaved = [JAVA_CRASH[0], other[0], JAVA_CRASH[1], other[1], JAVA_CRASH[2], other[2]]

        crashes, _ = _feed(interleaved)

        assert sorted(c.process for c in crashes) == ["com.android.settings", "com.example"]

    def test_a_header_whose_details_never_come_is_still_reported(self):
        """A crash cut short -- capture stopped, or the process was killed
        mid-write -- is still a crash."""
        crashes, detector = _feed(JAVA_CRASH[:1])
        assert crashes == []

        [crash] = detector.flush()
        assert crash.pid == 25084
        assert crash.process == "pid 25084"

    def test_another_line_from_the_same_pid_closes_the_block(self):
        crashes, _ = _feed([
            *JAVA_CRASH[:2],
            "2026-09-26 18:05:07.700 +0000 25084 25090 I Process: "
            "Sending signal. PID: 25084 SIG: 9",
        ])
        [crash] = crashes
        assert crash.process == "com.android.settings"

    def test_ordinary_androidruntime_lines_are_not_crashes(self):
        crashes, detector = _feed([
            "2026-09-26 18:00:00.000 +0000 100 100 D AndroidRuntime: Calling main entry com.x",
        ])
        assert crashes == [] and detector.flush() == []


class TestNativeCrashAndAnr:
    def test_a_fatal_signal_names_the_crashed_process(self):
        crashes, _ = _feed([
            "2026-09-26 18:05:07.000 +0000 1234 4321 F libc: Fatal signal 11 (SIGSEGV), code 1 "
            "(SEGV_MAPERR), fault addr 0x0 in tid 4321 (RenderThread), pid 1234 (com.example.app)",
        ])
        [crash] = crashes
        assert crash.source == LogSource.CRASH
        assert crash.process == "com.example.app"
        assert crash.pid == 1234

    def test_an_anr_is_reported(self):
        crashes, _ = _feed([
            "2026-09-26 18:05:07.000 +0000 700 720 E ActivityManager: "
            "ANR in com.example.app (com.example.app/.MainActivity)",
        ])
        [crash] = crashes
        assert crash.process == "com.example.app"
        assert "not responding" in crash.message


class TestTheAdapterEmitsThem:
    async def _run(self, lines, **kwargs):
        emitted = []

        async def collect(entry):
            emitted.append(entry)

        adapter = LogcatAdapter(serial="emulator-5554", on_entry=collect, **kwargs)

        class _Stdout:
            def __init__(self):
                self._lines = [(line + "\n").encode() for line in lines]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._lines:
                    raise StopAsyncIteration
                return self._lines.pop(0)

        class _Proc:
            stdout = _Stdout()

        adapter._process = _Proc()
        adapter._running = True
        await adapter._read_loop()
        return emitted

    async def test_the_crash_and_the_raw_lines_are_both_emitted(self):
        emitted = await self._run(JAVA_CRASH)

        assert [e.source for e in emitted].count(LogSource.CRASH) == 1
        assert [e.source for e in emitted].count(LogSource.LOGCAT) == len(JAVA_CRASH)

    async def test_a_process_filter_keeps_the_crash_of_the_app_it_names(self):
        """The crash is logged under the tag `AndroidRuntime`. Filtering it by
        tag, like every other line, would drop exactly the crash of the app
        the caller asked to watch."""
        emitted = await self._run(JAVA_CRASH, process_filter="com.android.settings")

        assert [e.process for e in emitted if e.source == LogSource.CRASH] == [
            "com.android.settings",
        ]

    async def test_a_crash_cut_off_by_the_stream_ending_is_emitted(self):
        emitted = await self._run(JAVA_CRASH[:1])
        assert [e.source for e in emitted].count(LogSource.CRASH) == 1


def test_an_android_crash_is_routed_to_the_crash_buffer():
    """The point of all of this: it lands where the firehose cannot evict it."""
    from server.main import buffer_for
    from server.storage.ring_buffer import RingBuffer

    [crash], _ = _feed(JAVA_CRASH)
    logs, crashes = RingBuffer(max_size=10), RingBuffer(max_size=10)

    assert buffer_for(crash, logs=logs, crashes=crashes) is crashes
