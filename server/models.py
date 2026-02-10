"""Core data models for log entries and API schemas."""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field


class LogLevel(str, enum.Enum):
    """Log severity levels, ordered from least to most severe."""

    DEBUG = "debug"
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    ERROR = "error"
    FAULT = "fault"

    @classmethod
    def at_least(cls, minimum: LogLevel) -> list[LogLevel]:
        """Return all levels at or above the given minimum."""
        ordered = list(cls)
        min_idx = ordered.index(minimum)
        return ordered[min_idx:]


class LogSource(str, enum.Enum):
    """Identifies which source adapter produced a log entry."""

    SYSLOG = "syslog"
    OSLOG = "oslog"
    CRASH = "crash"
    BUILD = "build"
    APP_DRAIN = "app_drain"


class LogEntry(BaseModel):
    """A single structured log entry. This is the core data type that flows through
    the entire system — from source adapters through processing to API responses."""

    id: str = Field(description="Unique entry identifier")
    timestamp: datetime
    device_id: str = Field(default="default", description="Device identifier (for future multi-device)")
    process: str = Field(default="", description="Process name (e.g., 'MyApp')")
    subsystem: str = Field(default="", description="OSLog subsystem (e.g., 'com.myapp.networking')")
    category: str = Field(default="", description="OSLog category (e.g., 'auth')")
    pid: int | None = Field(default=None, description="Process ID")
    level: LogLevel = LogLevel.INFO
    message: str
    source: LogSource
    raw: str = Field(default="", description="Original unparsed line, preserved for debugging")
    repeat_count: int = Field(
        default=1,
        description="Number of occurrences this entry represents. "
        "Values > 1 are emitted by the deduplicator for suppressed repeats.",
    )


class LogQueryParams(BaseModel):
    """Parameters for historical log queries."""

    since: datetime | None = None
    until: datetime | None = None
    level: LogLevel | None = None
    process: str | None = None
    source: LogSource | None = None
    search: str | None = None
    device_id: str = "default"
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class LogStreamParams(BaseModel):
    """Parameters for SSE log streaming."""

    level: LogLevel | None = None
    process: str | None = None
    subsystem: str | None = None
    category: str | None = None
    source: LogSource | None = None
    match: str | None = None
    exclude: str | None = None
    device_id: str = "default"


class SourceStatus(BaseModel):
    """Status of a log source adapter."""

    id: str
    type: str
    status: str  # "streaming", "watching", "stopped", "error"
    device_id: str = "default"
    entries_captured: int = 0
    started_at: datetime | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Summary / errors response models (Phase 1b)
# ---------------------------------------------------------------------------


class TopIssue(BaseModel):
    """A grouped error pattern with occurrence count."""

    pattern: str
    count: int
    first_seen: datetime
    last_seen: datetime
    resolved: bool = False


class LogSummaryResponse(BaseModel):
    """Response from GET /api/v1/logs/summary."""

    window: str
    generated_at: datetime
    cursor: str
    summary: str
    error_count: int
    warning_count: int
    total_count: int
    top_issues: list[TopIssue]


class LogErrorsResponse(BaseModel):
    """Response from GET /api/v1/logs/errors."""

    entries: list[LogEntry]
    total: int


# ---------------------------------------------------------------------------
# Crash report models (Phase 1c)
# ---------------------------------------------------------------------------


class CrashReport(BaseModel):
    """A parsed crash report."""

    crash_id: str = Field(description="Unique crash identifier")
    timestamp: datetime
    device_id: str = "default"
    process: str = Field(default="", description="Crashed process name")
    exception_type: str = Field(default="", description="e.g. EXC_BAD_ACCESS")
    exception_codes: str = Field(default="", description="e.g. KERN_INVALID_ADDRESS at 0x0")
    signal: str = Field(default="", description="e.g. SIGSEGV")
    top_frames: list[str] = Field(default_factory=list, description="Top stack frames from crashing thread")
    file_path: str = Field(default="", description="Path to the raw crash file on disk")
    raw_text: str = Field(default="", description="First portion of raw crash content")


class CrashLatestResponse(BaseModel):
    """Response from GET /api/v1/crashes/latest."""

    crashes: list[CrashReport]
    total: int


# ---------------------------------------------------------------------------
# Build result models (Phase 1c)
# ---------------------------------------------------------------------------


class BuildDiagnostic(BaseModel):
    """A single build error or warning."""

    file: str = ""
    line: int | None = None
    column: int | None = None
    severity: str = "error"  # "error" or "warning"
    message: str = ""


class TestFailure(BaseModel):
    """A single failing test case."""

    class_name: str = ""
    method: str = ""
    duration: float = 0.0
    message: str = ""


class TestSummary(BaseModel):
    """Summary of test execution."""

    passed: int = 0
    failed: int = 0
    total: int = 0
    duration: float = 0.0
    failures: list[TestFailure] = Field(default_factory=list)


class BuildResult(BaseModel):
    """Parsed result from an xcodebuild invocation."""

    succeeded: bool = False
    errors: list[BuildDiagnostic] = Field(default_factory=list)
    warnings: list[BuildDiagnostic] = Field(default_factory=list)
    tests: TestSummary | None = None
    raw_line_count: int = 0
