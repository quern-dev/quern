"""Source adapter for xcodebuild output parsing.

Unlike streaming adapters (syslog, oslog), this is an **on-demand** parser.
Callers submit raw xcodebuild output via ``parse_build_output()`` and receive
a structured ``BuildResult`` back.  Individual errors and warnings are also
emitted as ``LogEntry`` items through the normal pipeline.
"""

from __future__ import annotations

import logging
import pathlib
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from server.models import (
    BuildDiagnostic,
    BuildResult,
    LogEntry,
    LogLevel,
    LogSource,
    TestFailure,
    TestSummary,
    WarningGroup,
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

# An error with no source location: signing, provisioning, package resolution.
# `error: Signing for "App" requires a development team.` DIAGNOSTIC_RE wants
# file:line:col, so these parsed as nothing and a failed build read "0 errors".
LOCATIONLESS_ERROR_RE = re.compile(r"^(?:xcodebuild: )?error: (.+?)\s*$", re.MULTILINE)

# The step list xcodebuild prints after ** BUILD FAILED **. For a failure
# outside compilation it is the only statement of what failed: plug-in
# validation prints no error line at all, just the step (measured, Xcode 26.5).
FAILED_COMMANDS_RE = re.compile(
    r"^The following build commands failed:\n((?:[ \t]+\S.*\n?)+)", re.MULTILINE,
)
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_WHOLE_BUILD_RE = re.compile(r"^Building (?:workspace|project) .+ with scheme ")
_VALIDATION_RE = re.compile(r"^Validate (?:plug-in|macro)\b")
PLUGIN_VALIDATION_HINT = (
    "xcodebuild will not run a package plug-in or macro until it has been approved. "
    "Build the project once in Xcode and approve it when asked, or pass "
    "skip_plugin_validation=true to build_and_install, which adds "
    "-skipPackagePluginValidation and -skipMacroValidation to that build only"
)


def _located(diag: BuildDiagnostic) -> str:
    """`file:line:col: message`, or the message alone when it has no place."""
    if not diag.file:
        return diag.message
    return f"{diag.file}:{diag.line}:{diag.column}: {diag.message}"


def _failed_commands(content: str) -> list[str]:
    """xcodebuild's own list of what failed, less the noise in it: a base64
    token it prints under a plug-in step, and the whole-build step every
    failure lists."""
    block = FAILED_COMMANDS_RE.search(content)
    if not block:
        return []
    out = []
    for line in block.group(1).splitlines():
        step = line.strip()
        if step and not _BASE64_RE.fullmatch(step) and not _WHOLE_BUILD_RE.match(step):
            out.append(step)
    return out

# Matches: Test Suite 'All tests' passed at ... Executed N tests, with M failures ...
TEST_SUITE_SUMMARY_RE = re.compile(
    r"Executed (\d+) tests?, with (\d+) failures?"
)


_QUOTED_TOKEN_RE = re.compile(r"'[^']*'|\S+")

WILDCARD = "*"
MAX_WILDCARD_RATIO = 0.30


def _tokenize(message: str) -> list[str]:
    """Split on whitespace but keep single-quoted tokens intact."""
    return _QUOTED_TOKEN_RE.findall(message)


@dataclass
class _FuzzyTemplate:
    """A word-level template that accumulates warnings matching its pattern."""

    tokens: list[str]
    first_message: str
    files: list[str] = field(default_factory=list)
    count: int = 0


def _group_warnings_fuzzy(warnings: list[BuildDiagnostic]) -> list[WarningGroup]:
    """Group warnings by fuzzy word-level template matching.

    Two messages can merge if they have the same word count and differ in
    at most ~30% of positions.  Differing positions become wildcards.
    """
    templates: list[_FuzzyTemplate] = []

    for w in warnings:
        tokens = _tokenize(w.message)
        token_count = len(tokens)
        basename = pathlib.PurePosixPath(w.file).name if w.file else ""

        best_template: _FuzzyTemplate | None = None
        best_new_wildcards = token_count + 1  # worse than any real match

        for tpl in templates:
            if len(tpl.tokens) != token_count:
                continue

            new_wildcards = 0
            total_wildcards = 0
            for t_tok, m_tok in zip(tpl.tokens, tokens):
                if t_tok == WILDCARD:
                    total_wildcards += 1
                elif t_tok != m_tok:
                    new_wildcards += 1
                    total_wildcards += 1

            if token_count > 0 and total_wildcards / token_count > MAX_WILDCARD_RATIO:
                continue
            if new_wildcards < best_new_wildcards:
                best_new_wildcards = new_wildcards
                best_template = tpl

        if best_template is not None:
            # Merge: replace differing positions with wildcard
            for i, (t_tok, m_tok) in enumerate(
                zip(best_template.tokens, tokens)
            ):
                if t_tok != WILDCARD and t_tok != m_tok:
                    best_template.tokens[i] = WILDCARD
            best_template.count += 1
            if basename and basename not in best_template.files:
                best_template.files.append(basename)
        else:
            # Seed new template
            tpl = _FuzzyTemplate(
                tokens=list(tokens),
                first_message=w.message,
                count=1,
                files=[basename] if basename else [],
            )
            templates.append(tpl)

    return [
        WarningGroup(
            message=tpl.first_message,
            count=tpl.count,
            files=tpl.files[:5],
        )
        for tpl in templates
    ]


class BuildAdapter(BaseSourceAdapter):
    """On-demand xcodebuild output parser.

    This adapter does not have a continuous start/stop loop.  ``start()`` and
    ``stop()`` are no-ops — use ``parse_build_output()`` directly.
    """

    def __init__(
        self,
        device_id: str = "",
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

    async def parse_build_output(self, content: str, *, fuzzy: bool = True) -> BuildResult:
        """Parse raw xcodebuild output and return a structured result.

        Args:
            content: Raw xcodebuild output text.
            fuzzy: Use fuzzy word-level template grouping instead of exact-match.

        Also emits LogEntry items for each error/warning through the pipeline.
        """
        errors: list[BuildDiagnostic] = []
        raw_warnings: list[BuildDiagnostic] = []
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
                raw_warnings.append(diag)

        seen_errors = {e.message for e in errors}
        for m in LOCATIONLESS_ERROR_RE.finditer(content):
            if m.group(1) not in seen_errors:       # xcodebuild repeats them
                seen_errors.add(m.group(1))
                errors.append(BuildDiagnostic(message=m.group(1)))

        # Dedup warnings on (file, line, column, message)
        seen_warnings: set[tuple[str, int | None, int | None, str]] = set()
        warnings: list[BuildDiagnostic] = []
        for w in raw_warnings:
            key = (w.file, w.line, w.column, w.message)
            if key not in seen_warnings:
                seen_warnings.add(key)
                warnings.append(w)

        # Group deduped warnings
        if fuzzy:
            warning_groups = _group_warnings_fuzzy(warnings)
        else:
            # Exact-match grouping by message text
            group_files: OrderedDict[str, list[str]] = OrderedDict()
            group_counts: dict[str, int] = {}
            for w in warnings:
                basename = pathlib.PurePosixPath(w.file).name if w.file else ""
                group_counts[w.message] = group_counts.get(w.message, 0) + 1
                files = group_files.setdefault(w.message, [])
                if basename and basename not in files:
                    files.append(basename)

            warning_groups = [
                WarningGroup(
                    message=msg,
                    count=group_counts[msg],
                    files=files[:5],
                )
                for msg, files in group_files.items()
            ]

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

        # A failed build with nothing to show for it read "Build failed. 0
        # error(s)", which names no cause. Say what xcodebuild says failed.
        if not succeeded and not errors:
            for step in _failed_commands(content):
                message = f"{step} failed"
                if _VALIDATION_RE.match(step):
                    message += f": {PLUGIN_VALIDATION_HINT}"
                errors.append(BuildDiagnostic(message=message))

        result = BuildResult(
            succeeded=succeeded,
            errors=errors,
            warnings=warnings,
            warning_groups=warning_groups,
            warning_count=len(warnings),
            tests=tests,
            raw_line_count=content.count("\n") + 1,
        )
        self.latest_result = result

        # Emit log entries for errors and warnings
        now = datetime.now(UTC)
        for diag in errors:
            entry = LogEntry(
                id=uuid.uuid4().hex[:8],
                timestamp=now,
                device_id=self.device_id,
                process="xcodebuild",
                level=LogLevel.ERROR,
                message=_located(diag),
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
                message=_located(diag),
                source=LogSource.BUILD,
            )
            await self.emit(entry)

        return result
