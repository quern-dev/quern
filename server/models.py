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
    PROXY = "proxy"
    APP_DRAIN = "app_drain"
    SIMULATOR = "simulator"
    DEVICE = "device"
    SERVER = "server"


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
    device_id: str | None = None
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
    device_id: str | None = None


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


# ---------------------------------------------------------------------------
# Network proxy flow models (Phase 2)
# ---------------------------------------------------------------------------


class FlowRequest(BaseModel):
    """HTTP request captured by the proxy."""

    method: str
    url: str
    host: str
    path: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    body_size: int = 0
    body_truncated: bool = False
    body_encoding: str = "utf-8"


class FlowResponse(BaseModel):
    """HTTP response captured by the proxy."""

    status_code: int
    reason: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    body_size: int = 0
    body_truncated: bool = False
    body_encoding: str = "utf-8"


class FlowTiming(BaseModel):
    """Timing breakdown for a captured flow."""

    dns_ms: float | None = None
    connect_ms: float | None = None
    tls_ms: float | None = None
    request_ms: float | None = None
    response_ms: float | None = None
    total_ms: float | None = None


class FlowRecord(BaseModel):
    """A complete HTTP flow (request + response) captured by the proxy."""

    id: str = Field(description="Unique flow identifier")
    timestamp: datetime
    device_id: str = "default"
    request: FlowRequest
    response: FlowResponse | None = None
    timing: FlowTiming = Field(default_factory=FlowTiming)
    tls: dict[str, str] | None = None
    error: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_process: str | None = Field(default=None, description="Process that originated the request (e.g. nsurlsessiond)")
    source_pid: int | None = Field(default=None, description="PID of the originating process")
    simulator_udid: str | None = Field(default=None, description="Simulator UDID if traffic came from a simulator")


class FlowQueryParams(BaseModel):
    """Parameters for querying captured flows."""

    host: str | None = None
    path_contains: str | None = None
    method: str | None = None
    status_min: int | None = None
    status_max: int | None = None
    has_error: bool | None = None
    since: datetime | None = None
    until: datetime | None = None
    device_id: str = "default"
    simulator_udid: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class FlowQueryResponse(BaseModel):
    """Response from flow query endpoint."""

    flows: list[FlowRecord]
    total: int
    has_more: bool


# ---------------------------------------------------------------------------
# Proxy status & flow summary models (Phase 2b)
# ---------------------------------------------------------------------------


class ProxyStatusResponse(BaseModel):
    """Response from GET /api/v1/proxy/status."""

    status: str  # "running", "stopped", "error"
    port: int = 9101
    listen_host: str = "0.0.0.0"
    started_at: datetime | None = None
    flows_captured: int = 0
    active_filter: str | None = None
    active_intercept: str | None = None
    held_flows_count: int = 0
    mock_rules_count: int = 0
    error: str | None = None
    local_capture: list[str] = Field(default_factory=list)
    system_proxy: SystemProxyInfo | None = None
    cert_setup: dict[str, DeviceCertState] | None = None  # Per-device cert status


class SystemProxyInfo(BaseModel):
    """System proxy configuration status in API responses."""

    configured: bool
    interface: str | None = None
    original_state: str | None = None  # "enabled" or "disabled"


class SystemProxyRestoreInfo(BaseModel):
    """System proxy restore status in API responses."""

    restored: bool
    interface: str | None = None
    restored_to: str | None = None  # "enabled" or "disabled"


class HostSummary(BaseModel):
    """Traffic summary for a single host."""

    host: str
    total: int
    success: int = 0
    client_error: int = 0
    server_error: int = 0
    connection_errors: int = 0
    avg_latency_ms: float | None = None


class FlowErrorPattern(BaseModel):
    """A grouped error pattern with occurrence count."""

    pattern: str
    count: int
    first_seen: datetime
    last_seen: datetime


class SlowRequest(BaseModel):
    """A request that exceeded the slow threshold."""

    method: str
    url: str
    total_ms: float
    status_code: int | None = None


class FlowSummaryResponse(BaseModel):
    """Response from GET /api/v1/proxy/flows/summary."""

    window: str
    generated_at: datetime
    cursor: str
    summary: str
    total_flows: int
    by_host: list[HostSummary]
    errors: list[FlowErrorPattern]
    slow_requests: list[SlowRequest]


# ---------------------------------------------------------------------------
# Intercept models (Phase 2c)
# ---------------------------------------------------------------------------


class InterceptSetRequest(BaseModel):
    """Request body for POST /api/v1/proxy/intercept."""

    pattern: str


class HeldFlow(BaseModel):
    """A flow currently held by the intercept filter."""

    id: str
    held_at: datetime
    age_seconds: float
    request: FlowRequest


class InterceptStatusResponse(BaseModel):
    """Response from GET /api/v1/proxy/intercept/held."""

    pattern: str | None = None
    held_flows: list[HeldFlow] = Field(default_factory=list)
    total_held: int = 0


class ReleaseFlowRequest(BaseModel):
    """Request body for POST /api/v1/proxy/intercept/release."""

    flow_id: str
    modifications: dict | None = None  # {headers?, body?, url?, method?}


# ---------------------------------------------------------------------------
# Replay models (Phase 2c)
# ---------------------------------------------------------------------------


class ReplayRequest(BaseModel):
    """Request body for POST /api/v1/proxy/replay/{flow_id}."""

    modify_headers: dict[str, str] | None = None
    modify_body: str | None = None


class ReplayResponse(BaseModel):
    """Response from POST /api/v1/proxy/replay/{flow_id}."""

    status: str  # "success" or "error"
    original_flow_id: str
    status_code: int | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Mock models (Phase 2c)
# ---------------------------------------------------------------------------


class MockResponseSpec(BaseModel):
    """Specification for a mock HTTP response."""

    status_code: int = 200
    headers: dict[str, str] = Field(default_factory=lambda: {"content-type": "application/json"})
    body: str = ""


class SetMockRequest(BaseModel):
    """Request body for POST /api/v1/proxy/mocks."""

    pattern: str
    response: MockResponseSpec


class MockRuleInfo(BaseModel):
    """Info about an active mock rule."""

    rule_id: str
    pattern: str
    response: MockResponseSpec


class MockListResponse(BaseModel):
    """Response from GET /api/v1/proxy/mocks."""

    rules: list[MockRuleInfo] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# Device management models (Phase 3)
# ---------------------------------------------------------------------------


class DeviceType(str, enum.Enum):
    """Type of iOS device."""

    SIMULATOR = "simulator"
    DEVICE = "device"


class DeviceState(str, enum.Enum):
    """State of a device."""

    BOOTED = "booted"
    SHUTDOWN = "shutdown"
    BOOTING = "booting"


class DeviceInfo(BaseModel):
    """Information about a simulator or device."""

    udid: str
    name: str
    state: DeviceState
    device_type: DeviceType = DeviceType.SIMULATOR
    os_version: str = ""
    runtime: str = ""
    is_available: bool = True
    connection_type: str = ""  # "usb", "wifi", or "" for simulators
    device_family: str = ""  # "iPhone", "iPad", "Apple Watch", "Apple TV", or ""
    is_connected: bool = True  # True for simulators; physical devices: True when reachable (tunnel not "unavailable")


class AppInfo(BaseModel):
    """Information about an installed app."""

    bundle_id: str
    name: str = ""
    app_type: str = ""  # "User" or "System"
    architecture: str = ""
    install_type: str = ""
    process_state: str = ""


class DeviceError(Exception):
    """Raised when a device operation fails."""

    def __init__(self, message: str, tool: str = "unknown"):
        self.tool = tool
        super().__init__(message)


class BootDeviceRequest(BaseModel):
    """Request body for POST /device/boot."""

    udid: str | None = None
    name: str | None = None


class ShutdownDeviceRequest(BaseModel):
    """Request body for POST /device/shutdown."""

    udid: str


class InstallAppRequest(BaseModel):
    """Request body for POST /device/app/install."""

    app_path: str
    udid: str | None = None


class LaunchAppRequest(BaseModel):
    """Request body for POST /device/app/launch."""

    bundle_id: str
    udid: str | None = None


class TerminateAppRequest(BaseModel):
    """Request body for POST /device/app/terminate."""

    bundle_id: str
    udid: str | None = None


class UninstallAppRequest(BaseModel):
    """Request body for POST /device/app/uninstall."""

    bundle_id: str
    udid: str | None = None


# ---------------------------------------------------------------------------
# UI inspection models (Phase 3b)
# ---------------------------------------------------------------------------


class UIElement(BaseModel):
    """A single UI accessibility element from idb describe-all."""

    type: str  # "Button", "StaticText", "Slider", etc.
    label: str = ""  # from AXLabel
    identifier: str | None = None  # from AXUniqueId
    value: str | None = None  # from AXValue
    frame: dict[str, float] | None = None  # {"x", "y", "width", "height"}
    enabled: bool = True
    role: str = ""  # "AXButton", "AXSlider", etc.
    role_description: str = ""  # "button", "slider", etc.
    help: str | None = None
    custom_actions: list[str] = Field(default_factory=list)


class TapRequest(BaseModel):
    """Request body for POST /device/ui/tap."""

    x: float
    y: float
    udid: str | None = None


class TapElementRequest(BaseModel):
    """Request body for POST /device/ui/tap-element."""

    label: str | None = None
    identifier: str | None = None
    element_type: str | None = None
    udid: str | None = None
    skip_stability_check: bool = False  # Skip for static elements (tab bars, nav bars)


class SwipeRequest(BaseModel):
    """Request body for POST /device/ui/swipe."""

    start_x: float
    start_y: float
    end_x: float
    end_y: float
    duration: float = 0.5
    udid: str | None = None


class TypeTextRequest(BaseModel):
    """Request body for POST /device/ui/type."""

    text: str
    udid: str | None = None


class ClearTextRequest(BaseModel):
    """Request body for POST /device/ui/clear."""

    udid: str | None = None


class PressButtonRequest(BaseModel):
    """Request body for POST /device/ui/press."""

    button: str
    udid: str | None = None


class SetLocationRequest(BaseModel):
    """Request body for POST /device/location."""

    latitude: float
    longitude: float
    udid: str | None = None


class GrantPermissionRequest(BaseModel):
    """Request body for POST /device/permission."""

    bundle_id: str
    permission: str
    udid: str | None = None


class WaitCondition(str, enum.Enum):
    """Condition to wait for when polling an element."""

    EXISTS = "exists"
    NOT_EXISTS = "not_exists"
    VISIBLE = "visible"
    ENABLED = "enabled"
    DISABLED = "disabled"
    VALUE_EQUALS = "value_equals"
    VALUE_CONTAINS = "value_contains"


class WaitForElementRequest(BaseModel):
    """Request body for POST /device/ui/wait-for-element."""

    label: str | None = None
    identifier: str | None = None
    element_type: str | None = Field(default=None, alias="type")
    condition: WaitCondition
    value: str | None = None  # Required for value_* conditions
    timeout: float = Field(default=10, ge=0, le=60)  # ge=0 allows instant checks
    interval: float = Field(default=0.5, ge=0.1, le=5)
    udid: str | None = None


# ---------------------------------------------------------------------------
# Device pool models (Phase 4b)
# ---------------------------------------------------------------------------


class DeviceClaimStatus(str, enum.Enum):
    """Device claim state."""

    AVAILABLE = "available"
    CLAIMED = "claimed"


class DevicePoolEntry(BaseModel):
    """Single device in the pool."""

    udid: str
    name: str
    state: DeviceState
    device_type: DeviceType
    os_version: str
    runtime: str
    device_family: str = ""  # "iPhone", "iPad", "Apple Watch", "Apple TV", or ""

    claim_status: DeviceClaimStatus
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    last_used: datetime
    is_available: bool


class DevicePoolState(BaseModel):
    """Complete pool state for persistence."""

    version: str = "1.0"
    updated_at: datetime
    devices: dict[str, DevicePoolEntry]


# ---------------------------------------------------------------------------
# Resolution protocol models (Phase 4b-gamma)
# ---------------------------------------------------------------------------


class ResolveDeviceRequest(BaseModel):
    """Request body for POST /api/v1/devices/resolve."""

    udid: str | None = None
    name: str | None = None
    os_version: str | None = None
    device_family: str | None = None
    device_type: str | None = "simulator"
    auto_boot: bool = False
    wait_if_busy: bool = False
    wait_timeout: float = Field(default=30.0, ge=1.0, le=120.0)
    session_id: str | None = None


class EnsureDevicesRequest(BaseModel):
    """Request body for POST /api/v1/devices/ensure."""

    count: int = Field(ge=1, le=10)
    name: str | None = None
    os_version: str | None = None
    device_family: str | None = None
    device_type: str | None = "simulator"
    auto_boot: bool = True
    session_id: str | None = None


# ---------------------------------------------------------------------------
# Certificate management models (Phase 2 cert verification)
# ---------------------------------------------------------------------------


class DeviceCertState(BaseModel):
    """State of mitmproxy CA certificate installation for a device."""

    name: str
    cert_installed: bool = False
    fingerprint: str | None = None
    installed_at: str | None = None  # ISO 8601 timestamp
    verified_at: str | None = None  # Last SQLite check timestamp


class CertStatusResponse(BaseModel):
    """Response from GET /api/v1/proxy/cert/status."""

    cert_exists: bool
    cert_path: str
    fingerprint: str | None = None
    devices: dict[str, DeviceCertState] = Field(default_factory=dict)


class CertVerifyRequest(BaseModel):
    """Request body for POST /api/v1/proxy/cert/verify."""

    udid: str | None = None  # If None, verify all simulators (booted + shutdown)
    state: str | None = "booted"  # "booted", "shutdown", or None for all
    device_type: str | None = "simulator"  # "simulator", "device", or None for all


class DeviceCertInstallStatus(BaseModel):
    """Installation status for a single device."""

    udid: str
    name: str
    cert_installed: bool
    fingerprint: str | None = None
    verified_at: str  # ISO 8601 timestamp
    status: str = "unknown"  # "installed", "not_installed", "never_booted", "error"


class CertVerifyResponse(BaseModel):
    """Response from POST /api/v1/proxy/cert/verify."""

    verified: bool
    devices: list[DeviceCertInstallStatus]
    erased_devices: list[str] = Field(default_factory=list)  # UDIDs where erase was detected


class CertInstallRequest(BaseModel):
    """Request body for POST /api/v1/proxy/cert/install."""

    udid: str | None = None  # If None, install on all booted devices
    force: bool = False  # Force reinstall even if already installed


# ---------------------------------------------------------------------------
# Simulator logging models
# ---------------------------------------------------------------------------


class StartSimLogRequest(BaseModel):
    """Request body for POST /api/v1/device/logging/start."""

    udid: str | None = None
    process: str | None = None
    subsystem: str | None = None
    level: str = "debug"


class StopSimLogRequest(BaseModel):
    """Request body for POST /api/v1/device/logging/stop."""

    udid: str | None = None


class StartDeviceLogRequest(BaseModel):
    """Request body for POST /api/v1/device/logging/device/start."""

    udid: str | None = None
    process: str | None = None
    match: str | None = None


class StopDeviceLogRequest(BaseModel):
    """Request body for POST /api/v1/device/logging/device/stop."""

    udid: str | None = None


class SetupWdaRequest(BaseModel):
    """Request body for POST /api/v1/device/wda/setup."""

    udid: str
    team_id: str | None = None


class StartDriverRequest(BaseModel):
    """Request body for POST /api/v1/device/wda/start."""

    udid: str


class StopDriverRequest(BaseModel):
    """Request body for POST /api/v1/device/wda/stop."""

    udid: str
