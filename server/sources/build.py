"""Source adapter for xcodebuild output parsing.

Unlike streaming adapters (syslog, oslog), this is an **on-demand** parser.
Callers submit raw xcodebuild output via ``parse_build_output()`` and receive
a structured ``BuildResult`` back.  Individual errors and warnings are also
emitted as ``LogEntry`` items through the normal pipeline.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from server.models import (
    BuildDiagnostic,
    BuildResult,
    LogEntry,
    LogLevel,
    LogSource,
    TestFailure,
    TestSummary,
)
from server.sources import BaseSourceAdapter, EntryCallback

logger = logging.getLogger(__name__)

# Matches: /path/to/File.swift:42:10: error: use of undeclared identifier 'x'
DIAGNOSTIC_RE = re.compile(
    r"^(.+?):(\d+):(\d+):\s+(error|warning):\s+(.+)$", re.MULTILINE
)

# Matches: Test Case '-[SomeTests testFoo]' passed (0.123 seconds).
TEST_CASE_RE = re.compile(
    r"Test Case '-\[(\S+)\s+(\S+)\]' (passed|failed) \((\d+\.\d+) seconds\)\."
)

# Matches: ** BUILD SUCCEEDED ** or ** BUILD FAILED **
BUILD_STATUS_RE = re.compile(r"\*\*\s+BUILD\s+(SUCCEEDED|FAILED)\s+\*\*")

# Matches: Test Suite 'All tests' passed at ... Executed N tests, with M failures ...
TEST_SUITE_SUMMARY_RE = re.compile(
    r"Executed (\d+) tests?, with (\d+) failures?"
)


class BuildAdapter(BaseSourceAdapter):
    """On-demand xcodebuild output parser.

    This adapter does not have a continuous start/stop loop.  ``start()`` and
    ``stop()`` are no-ops — use ``parse_build_output()`` directly.
    """

    def __init__(
        self,
        device_id: str = "default",
        on_entry: EntryCallback | None = None,
    ) -> None:
        super().__init__(
            adapter_id="build",
            adapter_type="xcodebuild",
            device_id=device_id,
            on_entry=on_entry,
        )
        self.latest_result: BuildResult | None = None

    async def start(self) -> None:
        """No-op — build adapter is on-demand."""
        self._running = True
        self.started_at = self._now()

    async def stop(self) -> None:
        """No-op — build adapter is on-demand."""
        self._running = False

    def status(self):
        s = super().status()
        if s.status == "streaming":
            s.status = "ready"
        return s

    async def parse_build_output(self, content: str) -> BuildResult:
        """Parse raw xcodebuild output and return a structured result.

        Also emits LogEntry items for each error/warning through the pipeline.
        """
        errors: list[BuildDiagnostic] = []
        warnings: list[BuildDiagnostic] = []
        test_cases: list[tuple[str, str, str, float]] = []

        # Parse diagnostics (errors and warnings)
        for m in DIAGNOSTIC_RE.finditer(content):
            diag = BuildDiagnostic(
                file=m.group(1),
                line=int(m.group(2)),
                column=int(m.group(3)),
                severity=m.group(4),
                message=m.group(5),
            )
            if diag.severity == "error":
                errors.append(diag)
            else:
                warnings.append(diag)

        # Parse test results
        for m in TEST_CASE_RE.finditer(content):
            test_cases.append((
                m.group(1),  # class
                m.group(2),  # method
                m.group(3),  # passed/failed
                float(m.group(4)),  # duration
            ))

        # Build test summary
        tests: TestSummary | None = None
        if test_cases:
            failures = [
                TestFailure(
                    class_name=cls, method=method, duration=dur,
                )
                for cls, method, status, dur in test_cases
                if status == "failed"
            ]
            total_dur = sum(dur for _, _, _, dur in test_cases)
            tests = TestSummary(
                passed=sum(1 for _, _, s, _ in test_cases if s == "passed"),
                failed=len(failures),
                total=len(test_cases),
                duration=round(total_dur, 3),
                failures=failures,
            )

        # Determine overall success
        status_match = BUILD_STATUS_RE.search(content)
        succeeded = status_match.group(1) == "SUCCEEDED" if status_match else len(errors) == 0

        result = BuildResult(
            succeeded=succeeded,
            errors=errors,
            warnings=warnings,
            tests=tests,
            raw_line_count=content.count("\n") + 1,
        )
        self.latest_result = result

        # Emit log entries for errors and warnings
        now = datetime.now(timezone.utc)
        for diag in errors:
            entry = LogEntry(
                id=uuid.uuid4().hex[:8],
                timestamp=now,
                device_id=self.device_id,
                process="xcodebuild",
                level=LogLevel.ERROR,
                message=f"{diag.file}:{diag.line}:{diag.column}: {diag.message}",
                source=LogSource.BUILD,
            )
            await self.emit(entry)

        for diag in warnings:
            entry = LogEntry(
                id=uuid.uuid4().hex[:8],
                timestamp=now,
                device_id=self.device_id,
                process="xcodebuild",
                level=LogLevel.WARNING,
                message=f"{diag.file}:{diag.line}:{diag.column}: {diag.message}",
                source=LogSource.BUILD,
            )
            await self.emit(entry)

        return result
