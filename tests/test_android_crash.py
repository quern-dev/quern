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

    def test_a_second_header_from_the_same_pid_keeps_the_first_crash(self):
        """Mutation M29 in the review survived: dropping the flush on a
        repeated header lost the first crash silently."""
        crashes, detector = _feed([JAVA_CRASH[0], JAVA_CRASH[1], JAVA_CRASH[0]])
        [first] = crashes
        assert first.process == "com.android.settings"
        assert len(detector.flush()) == 1

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

    def test_an_anr_is_reported_with_the_apps_pid_not_system_servers(self):
        """The `ANR in` line is logged by system_server (pid 555 here); the
        app's pid is on the `PID:` line after it."""
        crashes, _ = _feed([
            "2026-09-26 18:05:07.000 +0000 555 720 E ActivityManager: "
            "ANR in com.example.app (com.example.app/.MainActivity)",
            "2026-09-26 18:05:07.000 +0000 555 720 E ActivityManager: PID: 4242",
            "2026-09-26 18:05:07.000 +0000 555 720 E ActivityManager: "
            "Reason: Input dispatching timed out",
        ])
        [crash] = crashes
        assert crash.process == "com.example.app"
        assert crash.pid == 4242
        assert "not responding" in crash.message

    def test_an_unrelated_system_server_line_does_not_close_the_anr(self):
        """CodeRabbit on #319: a `Start proc` line between `ANR in` and `PID:`
        closed the ANR early and lost the app's pid."""
        crashes, _ = _feed([
            "2026-09-26 18:05:07.000 +0000 555 720 E ActivityManager: ANR in com.example.app",
            "2026-09-26 18:05:07.001 +0000 555 721 I ActivityManager: "
            "Start proc 9000:com.other/u0a1",
            "2026-09-26 18:05:07.002 +0000 555 720 E ActivityManager: PID: 4242",
        ])
        [crash] = crashes
        assert crash.pid == 4242

    def test_an_anr_whose_pid_line_is_too_far_off_is_reported_without_it(self):
        from server.sources.android_crash import ANR_PID_WITHIN_LINES

        chatter = [
            f"2026-09-26 18:05:07.{i:03d} +0000 555 721 I ActivityManager: line {i}"
            for i in range(ANR_PID_WITHIN_LINES)
        ]
        crashes, _ = _feed([
            "2026-09-26 18:05:07.000 +0000 555 720 E ActivityManager: ANR in com.example.app",
            *chatter,
        ])
        [crash] = crashes
        assert crash.process == "com.example.app" and crash.pid is None

    def test_an_anr_whose_pid_line_never_comes_is_still_reported(self):
        crashes, detector = _feed([
            "2026-09-26 18:05:07.000 +0000 555 720 E ActivityManager: ANR in com.example.app",
        ])
        assert crashes == []
        [crash] = detector.flush()
        assert crash.process == "com.example.app" and crash.pid is None


class TestProcessFiltering:
    """A crash's name is not always the package name. Filtering it like an
    ordinary line discarded exactly the crash of the app the caller named."""

    def _native(self, comm):
        [crash], _ = _feed([
            "2026-09-26 18:05:07.000 +0000 1234 4321 F libc: Fatal signal 11 (SIGSEGV), code 1 "
            f"(SEGV_MAPERR), fault addr 0x0 in tid 4321 (RenderThread), pid 1234 ({comm})",
        ])
        return crash

    def test_a_native_crash_named_by_the_last_15_characters_matches(self):
        from server.sources.android_crash import process_matches

        crash = self._native("e.myapplication")  # com.example.myapplication
        assert process_matches("com.example.myapplication", crash)

    def test_a_short_name_is_not_matched_by_suffix(self):
        """Only a name at the 15-character cap can be a truncated tail."""
        from server.sources.android_crash import process_matches

        assert not process_matches("com.other.app", self._native("app"))

    def test_an_unnamed_java_crash_is_kept(self):
        from server.sources.android_crash import process_matches

        _, detector = _feed([JAVA_CRASH[0]])  # no Process: line
        [crash] = detector.flush()
        assert process_matches("com.example", crash)


class TestCrashSpecs:
    """liblog applies the last rule for a tag, so what is appended matters."""

    def test_a_silencing_filter_gets_every_crash_tag(self):
        from server.sources.android_crash import crash_specs_for

        assert crash_specs_for("MyTag:D *:S") == ["AndroidRuntime:E", "libc:F", "ActivityManager:E"]

    def test_a_tag_the_caller_named_is_left_as_they_set_it(self):
        """Appending AndroidRuntime:E narrowed a caller's AndroidRuntime:V."""
        from server.sources.android_crash import crash_specs_for

        assert crash_specs_for("AndroidRuntime:V *:S") == ["libc:F", "ActivityManager:E"]

    def test_without_a_default_rule_nothing_is_added(self):
        """The default level is verbose; adding specs only lowered tags the
        caller had left alone."""
        from server.sources.android_crash import crash_specs_for

        assert crash_specs_for("MyTag:D") == []


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

        class _Stderr:
            async def read(self):
                return b""

        class _Proc:
            stdout = _Stdout()
            stderr = _Stderr()
            returncode = 0

        adapter._process = _Proc()
        adapter._running = True
        await adapter._read_loop()
        # The fake stream ends without a stop, which is an error -- and it
        # must be *that* error. The fake had no stderr, so every run here
        # ended in an AttributeError nothing looked at (second review).
        assert adapter._error is not None and "ended unexpectedly" in adapter._error, adapter._error
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

    async def test_a_filtered_native_crash_of_the_named_app_is_emitted(self):
        """Verified by the review: zero crash entries before this fix."""
        emitted = await self._run([
            "2026-09-26 18:05:07.000 +0000 1234 4321 F libc: Fatal signal 11 (SIGSEGV), code 1 "
            "(SEGV_MAPERR), fault addr 0x0 in tid 4321 (RenderThread), pid 1234 (e.myapplication)",
        ], process_filter="com.example.myapplication")

        assert [e.source for e in emitted] == [LogSource.CRASH]

    async def test_a_tag_filter_keeps_the_crash_tags(self, monkeypatch):
        from tests.test_device_clock_is_utc import FakeAdb

        adb = FakeAdb(monkeypatch)
        await LogcatAdapter(serial="emulator-5554", tag_filter="MyTag:D *:S").start()

        for spec in ("AndroidRuntime:E", "libc:F", "ActivityManager:E"):
            assert spec in adb.logcat_args(), f"a tag filter silences {spec} at the device"

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
