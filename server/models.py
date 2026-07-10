"""Core data models for log entries and API schemas."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    LOGCAT = "logcat"
    PLIST_WATCHER = "plist_watcher"
    SERVER = "server"


class LogEntry(BaseModel):
    """A single structured log entry. This is the core data type that flows through
    the entire system — from source adapters through processing to API responses."""

    id: str = Field(description="Unique entry identifier")
    timestamp: datetime
    device_id: str = Field(
        default="default",
        description="Device identifier (for future multi-device)",
    )
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
    tail: bool = Field(
        default=False,
        description=(
            "If true, return the last N matching entries "
            "instead of paginating from offset"
        ),
    )


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
    top_frames: list[str] = Field(
        default_factory=list,
        description="Top stack frames from crashing thread",
    )
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


class WarningGroup(BaseModel):
    """Warnings grouped by message pattern."""

    message: str
    count: int
    files: list[str] = Field(default_factory=list)


class BuildResult(BaseModel):
    """Parsed result from an xcodebuild invocation."""

    succeeded: bool = False
    errors: list[BuildDiagnostic] = Field(default_factory=list)
    warnings: list[BuildDiagnostic] = Field(default_factory=list)
    warning_groups: list[WarningGroup] = Field(default_factory=list)
    warning_count: int = 0
    tests: TestSummary | None = None
    raw_line_count: int = 0
    summary: str = ""

    def generate_summary(self) -> str:
        """Generate a concise text summary of the build result."""
        if not self.succeeded:
            parts = [f"Build failed. {len(self.errors)} error(s)."]
            for err in self.errors[:5]:
                loc = err.file
                if err.line:
                    loc += f":{err.line}"
                parts.append(f"  {loc}: {err.message}")
            if len(self.errors) > 5:
                parts.append(f"  ... and {len(self.errors) - 5} more")
            return "\n".join(parts)

        parts = ["Build succeeded."]
        if self.warning_count == 0:
            parts.append(f"Clean build, {self.raw_line_count} lines parsed.")
        elif self.warning_groups and len(self.warning_groups) < self.warning_count:
            parts.append(
                f"{self.warning_count} warnings "
                f"({len(self.warning_groups)} unique groups), "
                f"{self.raw_line_count} lines parsed."
            )
        else:
            parts.append(f"{self.warning_count} warning(s), {self.raw_line_count} lines parsed.")

        if self.tests:
            t = self.tests
            parts.append(f"Tests: {t.passed} passed, {t.failed} failed ({t.duration:.1f}s).")
            for f in t.failures[:3]:
                parts.append(f"  FAIL: {f.class_name}.{f.method}: {f.message}")
            if len(t.failures) > 3:
                parts.append(f"  ... and {len(t.failures) - 3} more failures")

        return " ".join(parts)


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
    source_process: str | None = Field(
        default=None,
        description="Process that originated the request (e.g. nsurlsessiond)",
    )
    source_pid: int | None = Field(
        default=None,
        description="PID of the originating process",
    )
    simulator_udid: str | None = Field(
        default=None,
        description="Simulator UDID if traffic came from a simulator",
    )
    client_ip: str | None = Field(
        default=None,
        description="Client IP address (for physical device identification)",
    )


class FlowQueryParams(BaseModel):
    """Parameters for querying captured flows."""

    host: str | None = None
    hosts: list[str] | None = None
    exclude_hosts: list[str] | None = None
    path_contains: str | None = None
    method: str | None = None
    status_min: int | None = None
    status_max: int | None = None
    has_error: bool | None = None
    since: datetime | None = None
    until: datetime | None = None
    device_id: str = "default"
    simulator_udid: str | None = None
    client_ip: str | None = None
    detail: Literal["full", "summary"] = "full"
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class FlowSummaryItem(BaseModel):
    """Lightweight flow representation for detail='summary' responses."""

    id: str
    timestamp: datetime
    method: str
    url: str
    host: str
    path: str
    status_code: int | None = None
    error: str | None = None
    total_ms: float | None = None


class FlowQueryResponse(BaseModel):
    """Response from flow query endpoint."""

    flows: list[FlowRecord] = []
    flow_summaries: list[FlowSummaryItem] | None = None
    total: int
    has_more: bool


class CaptureStartRequest(BaseModel):
    """Request body for POST /api/v1/proxy/capture/start."""

    id: str | None = None
    hosts: list[str] | None = None
    exclude_hosts: list[str] | None = None
    simulator_udid: str | None = None
    client_ip: str | None = None
    detail: Literal["full", "summary"] = "full"


class CaptureStartResponse(BaseModel):
    """Response from POST /api/v1/proxy/capture/start."""

    session_id: str
    start_time: datetime


class CaptureStopRequest(BaseModel):
    """Request body for POST /api/v1/proxy/capture/stop."""

    session_id: str


class CaptureStopResponse(BaseModel):
    """Response from POST /api/v1/proxy/capture/stop."""

    session_id: str
    duration_seconds: float
    total_flows: int
    flows: list[FlowRecord] = []
    flow_summaries: list[FlowSummaryItem] | None = None
    by_host: list[HostSummary] = []


class WaitForFlowRequest(BaseModel):
    """Request body for POST /api/v1/proxy/flows/wait."""

    host: str | None = None
    path_contains: str | None = None
    method: str | None = None
    status_min: int | None = None
    status_max: int | None = None
    has_error: bool | None = None
    simulator_udid: str | None = None
    client_ip: str | None = None
    timeout: float = Field(default=10, ge=0.1, le=60)
    interval: float = Field(default=0.5, ge=0.1, le=5)
    since: datetime | None = None  # defaults to now - 5s if omitted


class WaitForFlowResponse(BaseModel):
    """Response from POST /api/v1/proxy/flows/wait."""

    matched: bool
    flow: FlowRecord | None = None
    elapsed_seconds: float
    polls: int


# ---------------------------------------------------------------------------
# Proxy status & flow summary models (Phase 2b)
# ---------------------------------------------------------------------------


class InterfaceInfo(BaseModel):
    """One active Mac network interface with its IPv4 address.

    Returned in ``ProxyStatusResponse.local_ips`` to disambiguate which Mac
    IP to advertise when the host has multiple interfaces on different
    subnets (Wi-Fi + Ethernet to different networks). The default-route
    interface — what ``local_ip`` reflects — is correct for outbound
    traffic but not necessarily reachable from a physical device on the
    *other* interface's subnet.
    """

    interface: str  # BSD device name (e.g. "en0", "en10")
    ip: str  # IPv4 address
    subnet: str | None = None  # /24 containing `ip`, e.g. "192.168.31.0/24"
    is_default_route: bool = False  # OS default-route interface (matches `local_ip`)
    ssid: str | None = None  # Wi-Fi SSID when the interface is associated, else None


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
    bypass_patterns: list[str] = Field(default_factory=list)
    error: str | None = None
    local_capture: list[str] = Field(default_factory=list)
    local_ip: str | None = None
    """OS default-route IP — one entry from ``local_ips``. Convenient when
    there's only one interface; misleading in dual-interface setups. Prefer
    ``local_ips`` and per-device subnet matching when configuring a
    physical device's manual proxy."""
    local_ips: list[InterfaceInfo] = Field(default_factory=list)
    """Every active non-loopback IPv4 interface on the Mac. Pick the entry
    whose ``subnet`` matches the device's LAN IP when configuring a
    physical device's Wi-Fi proxy — the device must be on the same subnet
    as the Mac IP it talks to."""
    warnings: list[str] = Field(default_factory=list)
    """Network-state warnings the agent should surface. Currently:
    ``"multi_interface_active"`` — more than one interface is on a distinct
    /24, so ``local_ip`` is not the right answer for every device."""
    system_proxy: SystemProxyInfo | None = None
    cert_setup: dict[str, DeviceCertState] | None = None  # Per-device cert status
    network_state: dict | None = None
    """Snapshot of the Mac's network identity from the background monitor —
    current SSID, current Mac IP, last-changed timestamp, and a small
    history of recent changes. When the laptop moves between networks, the
    monitor notices within ~15 seconds and updates this field; combined
    with the per-device wifi_proxy_stale flag in cert_setup, an agent
    reading this response sees both *what changed* and *which devices
    need their proxy reconfigured*."""


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


class FlowEvent(BaseModel):
    """Lightweight flow event for SSE streaming.

    Contains enough to render a list item in a UI. Clients fetch
    full details via GET /proxy/flows/{id} when needed.
    """

    id: str
    timestamp: datetime
    method: str
    url: str
    host: str
    path: str
    status_code: int | None = None
    error: str | None = None
    duration_ms: float | None = None
    request_size: int = 0
    response_size: int = 0
    device_id: str = "default"
    simulator_udid: str | None = None
    source_process: str | None = None

    @classmethod
    def from_flow(cls, flow: FlowRecord) -> FlowEvent:
        return cls(
            id=flow.id,
            timestamp=flow.timestamp,
            method=flow.request.method,
            url=flow.request.url,
            host=flow.request.host,
            path=flow.request.path,
            status_code=(
                flow.response.status_code if flow.response else None
            ),
            error=flow.error,
            duration_ms=flow.timing.total_ms,
            request_size=flow.request.body_size,
            response_size=(
                flow.response.body_size if flow.response else 0
            ),
            device_id=flow.device_id,
            simulator_udid=flow.simulator_udid,
            source_process=flow.source_process,
        )


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
    """Request body for POST /api/v1/proxy/mocks.

    Accepts both nested and flat shapes:
      Nested: {"pattern": "...", "response": {"status_code": 200, "body": "..."}}
      Flat:   {"pattern": "...", "status_code": 200, "body": "..."}
    """

    pattern: str
    response: MockResponseSpec | None = None
    status_code: int | None = None
    headers: dict[str, str] | None = None
    body: str | None = None

    @model_validator(mode="after")
    def wrap_flat_fields(self) -> SetMockRequest:
        if self.response is None:
            self.response = MockResponseSpec(
                status_code=self.status_code if self.status_code is not None else 200,
                headers=self.headers or {"content-type": "application/json"},
                body=self.body or "",
            )
        self.status_code = None
        self.headers = None
        self.body = None
        return self


class UpdateMockRequest(BaseModel):
    """Request body for PATCH /api/v1/proxy/mocks/{rule_id}.

    Accepts both nested and flat shapes, same as SetMockRequest.
    """

    pattern: str | None = None
    response: MockResponseSpec | None = None
    status_code: int | None = None
    headers: dict[str, str] | None = None
    body: str | None = None

    @model_validator(mode="after")
    def wrap_flat_fields(self) -> UpdateMockRequest:
        if self.response is None and (
            self.status_code is not None
            or self.headers is not None
            or self.body is not None
        ):
            self.response = MockResponseSpec(
                status_code=self.status_code if self.status_code is not None else 200,
                headers=self.headers or {"content-type": "application/json"},
                body=self.body or "",
            )
        self.status_code = None
        self.headers = None
        self.body = None
        return self


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
    """Type of device."""

    SIMULATOR = "simulator"
    DEVICE = "device"
    ANDROID_EMULATOR = "android_emulator"
    ANDROID_DEVICE = "android_device"


class DeviceState(str, enum.Enum):
    """State of a device."""

    BOOTED = "booted"
    SHUTDOWN = "shutdown"
    BOOTING = "booting"
    UNAUTHORIZED = "unauthorized"


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
    # True for simulators; physical devices: True when
    # reachable (tunnel not "unavailable")
    is_connected: bool = True


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


class WdaError(DeviceError):
    """Base for all WDA-specific errors. Carries the W3C error code."""

    def __init__(self, message: str, wda_error: str = "", wda_message: str = ""):
        self.wda_error = wda_error
        self.wda_message = wda_message
        super().__init__(message, tool="wda")


class WdaInvalidSessionError(WdaError): ...
class WdaElementNotFoundError(WdaError): ...
class WdaStaleElementError(WdaError): ...
class WdaKeyboardNotPresentError(WdaError): ...
class WdaElementNotInteractableError(WdaError): ...
class WdaAppCrashedError(WdaError): ...


class BootDeviceRequest(BaseModel):
    """Request body for POST /device/boot."""

    udid: str | None = None
    name: str | None = None
    headless: bool = False


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
    env: dict[str, str] | None = None
    include_screen_context: bool = False
    capture_screenshots: bool = False
    settle_delay: float = Field(default=1.0, ge=0, le=10)


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
    extra_attrs: dict[str, str] | None = None
    """Raw source attributes from the underlying accessibility provider, kept
    verbatim before the per-platform normalizer collapses them. Useful for
    debugging the normalizer itself — e.g., checking whether an Android node
    has selected="true" without reaching for adb shell uiautomator dump.
    Populated only for Android (uiautomator2 XML attributes); idb output on
    iOS is already in our canonical shape, so this is None there. Stripped
    from API responses unless include_raw=true is passed, to keep payloads
    small in the common case."""


# ---------------------------------------------------------------------------
# Screen landmarks
# ---------------------------------------------------------------------------


class Landmark(BaseModel):
    """A single element selector for screen identification.

    Identifier is the primary match field (locale-independent).
    Label is a fallback for elements without stable identifiers.
    """

    element: str  # element type (required, e.g. "navigationBar", "Button")
    identifier: str | None = None  # primary: exact match, case-sensitive
    label: str | None = None  # fallback: exact match, case-insensitive
    label_contains: str | None = None  # fallback: substring match, case-insensitive
    absent: bool = False  # if True, element must NOT be present
    selected: bool | None = None
    """Selection state for selectable elements (RadioButton, Switch, CheckBox,
    Tab). When set, the element's selection state must match — useful for
    distinguishing "the Timelines tab is the selected one" from "the
    Timelines tab is just present." Both iOS and Android backends normalize
    selection state into UIElement.value as "1" (selected) / "0" (not).
    Omit to ignore selection state."""


class ScreenLandmarks(BaseModel):
    """Named screen with its identifying landmarks."""

    screen: str  # screen name
    landmarks: list[Landmark]


class LoadLandmarksRequest(BaseModel):
    """Request body for POST /landmarks/load."""

    app: str  # app identifier (e.g. bundle ID)
    source: str | None = None  # path to knowledge base directory
    landmarks: dict[str, list[dict]] | None = None  # inline: screen_name -> landmarks


class IdentifyRequest(BaseModel):
    """Request body for POST /landmarks/identify."""

    app: str | None = None  # scope to specific app (omit = match all)
    udid: str | None = None
    mode: str | None = None
    snapshot_depth: int | None = None
    source_timeout: float | None = None


class TapRequest(BaseModel):
    """Request body for POST /device/ui/tap."""

    x: float
    y: float
    udid: str | None = None


class TapElementRequest(BaseModel):
    """Request body for POST /device/ui/tap-element."""

    label: str | None = None
    label_contains: str | None = None
    label_prefix: str | None = None
    identifier: str | None = None
    element_type: str | None = None
    udid: str | None = None
    skip_stability_check: bool = False  # Skip for static elements (tab bars, nav bars)
    source_timeout: float | None = None  # Override WDA /source timeout (1-60s)
    value: str | None = None  # For switches: "0"=off, "1"=on. Skips tap if matched.
    include_screen_context: bool = False
    capture_screenshots: bool = False
    settle_delay: float = Field(default=1.0, ge=0, le=10)

    @model_validator(mode="after")
    def check_label_exclusivity(self):
        label_params = [
            p for p in (self.label, self.label_contains, self.label_prefix)
            if p is not None
        ]
        if len(label_params) > 1:
            raise ValueError("Only one of label, label_contains, or label_prefix may be provided")
        return self


class SwipeRequest(BaseModel):
    """Request body for POST /device/ui/swipe."""

    start_x: float
    start_y: float
    end_x: float
    end_y: float
    duration: float = 0.5
    udid: str | None = None


class ScrollToElementRequest(BaseModel):
    """Request body for POST /device/ui/scroll-to-element."""

    label: str | None = None
    identifier: str | None = None
    udid: str | None = None
    max_swipes: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def check_target(self):
        if not self.label and not self.identifier:
            raise ValueError("Either label or identifier is required")
        return self


class TypeTextRequest(BaseModel):
    """Request body for POST /device/ui/type."""

    text: str
    udid: str | None = None
    include_screen_context: bool = False
    capture_screenshots: bool = False
    settle_delay: float = Field(default=1.0, ge=0, le=10)


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


class OpenUrlRequest(BaseModel):
    """Request body for POST /device/open-url."""

    url: str
    udid: str | None = None
    bundle_id: str | None = None
    include_screen_context: bool = False
    capture_screenshots: bool = False
    settle_delay: float = Field(default=1.0, ge=0, le=10)


class GrantPermissionRequest(BaseModel):
    """Request body for POST /device/permission."""

    bundle_id: str
    permission: str
    udid: str | None = None


class SetLocaleRequest(BaseModel):
    """Request body for POST /device/locale."""

    lang: str
    country: str = ""
    udid: str | None = None


class SetHardwareKeyboardRequest(BaseModel):
    """Request body for POST /device/keyboard."""

    enabled: bool
    udid: str | None = None


class SetFontScaleRequest(BaseModel):
    """Request body for POST /device/font-scale."""

    scale: float
    udid: str | None = None


class SetDisplayDensityRequest(BaseModel):
    """Request body for POST /device/display-density."""

    dpi: int | None = None  # None = reset to default
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
    label_contains: str | None = None
    label_prefix: str | None = None
    identifier: str | None = None
    element_type: str | None = Field(default=None, alias="type")
    condition: WaitCondition
    value: str | None = None  # Required for value_* conditions
    timeout: float = Field(default=10, ge=0, le=60)  # ge=0 allows instant checks
    interval: float = Field(default=0.5, ge=0.1, le=5)
    udid: str | None = None
    mode: str | None = None  # "flat" for custom companion flat mode

    @model_validator(mode="after")
    def check_label_exclusivity(self):
        label_params = [
            p for p in (self.label, self.label_contains, self.label_prefix)
            if p is not None
        ]
        if len(label_params) > 1:
            raise ValueError("Only one of label, label_contains, or label_prefix may be provided")
        return self


# ---------------------------------------------------------------------------
# Device pool models (Phase 4b)
# ---------------------------------------------------------------------------


class DevicePoolEntry(BaseModel):
    """Single device in the pool."""

    udid: str
    name: str
    state: DeviceState
    device_type: DeviceType
    os_version: str
    runtime: str
    device_family: str = ""  # "iPhone", "iPad", "Apple Watch", "Apple TV", or ""

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
    auto_boot: bool = True


class EnsureDevicesRequest(BaseModel):
    """Request body for POST /api/v1/devices/ensure."""

    count: int = Field(ge=1, le=10)
    name: str | None = None
    os_version: str | None = None
    device_family: str | None = None
    device_type: str | None = "simulator"
    auto_boot: bool = True


# ---------------------------------------------------------------------------
# Certificate management models (Phase 2 cert verification)
# ---------------------------------------------------------------------------


class WifiProxyNetworkConfig(BaseModel):
    """Proxy config recorded for a single Wi-Fi network (keyed by SSID)."""

    proxy_host: str
    proxy_port: int
    client_ip: str | None = None
    set_at: str | None = None


class DeviceCertState(BaseModel):
    """State of mitmproxy CA certificate installation for a device."""

    name: str
    cert_installed: bool = False
    fingerprint: str | None = None
    installed_at: str | None = None  # ISO 8601 timestamp
    verified_at: str | None = None  # Last SQLite check timestamp
    # Per-network proxy config (keyed by SSID)
    wifi_proxy_configs: dict[str, WifiProxyNetworkConfig] | None = None
    # Computed at read time
    wifi_proxy_stale: bool = False
    active_wifi_network: str | None = None  # SSID whose config is currently active


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
    preset: str | None = None


class StopSimLogRequest(BaseModel):
    """Request body for POST /api/v1/device/logging/stop."""

    udid: str | None = None


class StartDeviceLogRequest(BaseModel):
    """Request body for POST /api/v1/device/logging/device/start."""

    udid: str | None = None
    process: str | None = None
    match: str | None = None
    preset: str | None = None


class StopDeviceLogRequest(BaseModel):
    """Request body for POST /api/v1/device/logging/device/stop."""

    udid: str | None = None


# ---------------------------------------------------------------------------
# Host oslog streaming models
# ---------------------------------------------------------------------------


class StartOslogRequest(BaseModel):
    """Request body for POST /api/v1/logs/oslog/start."""

    subsystem: str | None = None
    process: str | None = None


class PreviewStartRequest(BaseModel):
    """Request body for POST /api/v1/device/preview/start."""

    udid: str | None = None


class PreviewStopRequest(BaseModel):
    """Request body for POST /api/v1/device/preview/stop."""

    udid: str | None = None


class SetupWdaRequest(BaseModel):
    """Request body for POST /api/v1/device/wda/setup."""

    udid: str
    team_id: str | None = None
    force: bool = False


class StartDriverRequest(BaseModel):
    """Request body for POST /api/v1/device/wda/start."""

    udid: str


class StopDriverRequest(BaseModel):
    """Request body for POST /api/v1/device/wda/stop."""

    udid: str


# ---------------------------------------------------------------------------
# App state checkpoint + plist models
# ---------------------------------------------------------------------------


class SaveAppStateRequest(BaseModel):
    """Request body for POST /api/v1/device/app/state/save."""

    bundle_id: str
    label: str
    description: str | None = None
    udid: str | None = None


class RestoreAppStateRequest(BaseModel):
    """Request body for POST /api/v1/device/app/state/restore."""

    bundle_id: str
    label: str
    udid: str | None = None


class ReadAppPlistRequest(BaseModel):
    """Request body for GET /api/v1/device/app/state/plist (query params only)."""

    bundle_id: str
    container: str
    plist_path: str
    key: str | None = None
    udid: str | None = None


class SetAppPlistValueRequest(BaseModel):
    """Request body for POST /api/v1/device/app/state/plist."""

    bundle_id: str
    container: str
    plist_path: str
    key: str
    value: object
    udid: str | None = None


class SetAppPlistValuesRequest(BaseModel):
    """Request body for POST /api/v1/device/app/state/plist/batch."""

    bundle_id: str
    container: str
    plist_path: str
    values: dict[str, object]  # key → value mapping
    udid: str | None = None


class StartPlistWatchRequest(BaseModel):
    """Request body for POST /api/v1/device/app/state/plist/watch/start."""

    bundle_id: str
    container: str
    plist_path: str
    udid: str | None = None
    poll_interval: float = Field(default=1.0, ge=0.2, le=30.0)
    ignore_prefixes: list[str] = Field(default_factory=list)


class StopPlistWatchRequest(BaseModel):
    """Request body for POST /api/v1/device/app/state/plist/watch/stop."""

    bundle_id: str
    container: str
    plist_path: str
    udid: str | None = None


class PlistWatchTarget(BaseModel):
    """A single plist to watch."""

    container: str
    plist_path: str
    ignore_prefixes: list[str] = Field(default_factory=list)


class ConfigurePlistWatchRequest(BaseModel):
    """Request body for POST /api/v1/device/app/state/plist/watch/configure."""

    bundle_id: str
    watches: list[PlistWatchTarget]


class ClearPlistWatchConfigRequest(BaseModel):
    """Request body for DELETE /api/v1/device/app/state/plist/watch/configure."""

    bundle_id: str


class DeleteAppPlistKeyRequest(BaseModel):
    """Request body for DELETE /api/v1/device/app/state/plist/key."""

    bundle_id: str
    container: str
    plist_path: str
    key: str
    udid: str | None = None
