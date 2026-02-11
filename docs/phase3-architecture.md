# Phase 3: Device Inspection & Control

## Architecture Document — v0.1

---

## 1. Vision

Add remote iOS device inspection and control capabilities so an AI agent can see what's on screen, read the UI hierarchy, and interact with the app — tap buttons, type text, swipe through views — all through the same HTTP API and MCP interface used for logs and network traffic.

The end-to-end story: an AI agent sees an error in the logs (Phase 1), inspects the failed network request (Phase 2), takes a screenshot to see what the user is experiencing, reads the accessibility tree to understand the UI state, and taps a retry button to test the fix — all without a human touching the device.

---

## 2. Tool Landscape

iOS UI automation requires XCTest under the hood. Apple's CLI tools (`simctl`/`devicectl`) handle device management but cannot interact with UI. Here's how we layer the tools:

| Layer | Simulator | Physical Device |
|-------|-----------|-----------------|
| **Device Management** | `xcrun simctl` (boot, shutdown, install, launch, screenshot, keychain, location, permissions) | `xcrun devicectl` (install, launch, terminate) |
| **UI Automation** | `idb` via `idb_companion` + `fb-idb` (accessibility tree, tap, swipe, type) | WebDriverAgent (Phase 3d, deferred) |

### Why idb for simulators?

- **No on-device installation.** `idb_companion` runs on macOS and talks to Apple's private frameworks from the outside. Nothing gets installed on the simulator.
- **CLI interface.** We can call `idb` commands as subprocesses, matching our established pattern.
- **Rich primitives.** `idb ui describe-all` for accessibility tree, `idb ui tap`, `idb ui swipe`, `idb ui text` for interaction.
- **Proven at scale.** Built by Meta for their iOS device farm infrastructure.

### idb Architecture (for reference)

```
Our Server → fb-idb (Python CLI) → gRPC → idb_companion (macOS daemon) → Apple Private APIs → Simulator
```

We'll call `idb` CLI commands as subprocesses rather than importing fb-idb as a Python library, keeping the boundary clean and avoiding asyncio compatibility issues.

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                     iOS Debug Server (port 9100)                      │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │                       HTTP API Layer                          │    │
│  │                                                               │    │
│  │  ── Phase 1 (Logs) ──────────────────────────────────────     │    │
│  │  GET  /api/v1/logs/*                                          │    │
│  │                                                               │    │
│  │  ── Phase 2 (Proxy) ─────────────────────────────────────     │    │
│  │  GET  /api/v1/proxy/*                                         │    │
│  │                                                               │    │
│  │  ── Phase 3 (Device) ────────────────────────────────────     │    │
│  │  GET  /api/v1/device/list           List simulators/devices   │    │
│  │  POST /api/v1/device/boot           Boot a simulator          │    │
│  │  POST /api/v1/device/shutdown       Shutdown a simulator      │    │
│  │  POST /api/v1/device/app/install    Install an app            │    │
│  │  POST /api/v1/device/app/launch     Launch an app             │    │
│  │  POST /api/v1/device/app/terminate  Terminate an app          │    │
│  │  GET  /api/v1/device/app/list       List installed apps       │    │
│  │  GET  /api/v1/device/screenshot     Capture screenshot        │    │
│  │  GET  /api/v1/device/ui             Get accessibility tree    │    │
│  │  POST /api/v1/device/ui/tap         Tap at coordinates        │    │
│  │  POST /api/v1/device/ui/tap-element Tap by accessibility ID   │    │
│  │  POST /api/v1/device/ui/swipe       Swipe gesture             │    │
│  │  POST /api/v1/device/ui/type        Type text                 │    │
│  │  POST /api/v1/device/ui/press       Press hardware button     │    │
│  │  POST /api/v1/device/location       Set GPS location          │    │
│  │  POST /api/v1/device/permission     Grant app permission      │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │                    Device Controller                           │    │
│  │                                                               │    │
│  │  DeviceController (orchestrates all device operations)         │    │
│  │    │                                                          │    │
│  │    ├── SimctlBackend                                          │    │
│  │    │   └── calls: xcrun simctl (boot, install, launch,        │    │
│  │    │       screenshot, keychain, location, privacy)            │    │
│  │    │                                                          │    │
│  │    ├── IdbBackend                                             │    │
│  │    │   └── calls: idb (ui describe-all, ui tap, ui swipe,    │    │
│  │    │       ui text, ui key)                                    │    │
│  │    │                                                          │    │
│  │    └── DevicectlBackend (Phase 3d)                            │    │
│  │        └── calls: xcrun devicectl (physical device mgmt)      │    │
│  │                                                               │    │
│  │  Each backend runs commands as async subprocesses.             │    │
│  │  DeviceController selects backend based on target type.        │    │
│  └──────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 4. Device Controller Design

Unlike Phases 1 and 2, Phase 3 doesn't have a long-running subprocess to manage. Instead, it dispatches individual commands to CLI tools. The DeviceController is the orchestrator that routes operations to the right backend.

### 4.1 Backend Interface

```python
class DeviceBackend(ABC):
    """Abstract interface for device operations."""
    
    @abstractmethod
    async def list_devices(self) -> list[DeviceInfo]: ...
    
    @abstractmethod
    async def boot(self, udid: str) -> None: ...
    
    @abstractmethod
    async def shutdown(self, udid: str) -> None: ...
    
    @abstractmethod
    async def install_app(self, udid: str, app_path: str) -> None: ...
    
    @abstractmethod
    async def launch_app(self, udid: str, bundle_id: str) -> None: ...
    
    @abstractmethod
    async def terminate_app(self, udid: str, bundle_id: str) -> None: ...
    
    @abstractmethod
    async def list_apps(self, udid: str) -> list[AppInfo]: ...
    
    @abstractmethod
    async def screenshot(self, udid: str) -> bytes: ...
    
    @abstractmethod
    async def get_ui_tree(self, udid: str) -> UITree: ...
    
    @abstractmethod
    async def tap(self, udid: str, x: float, y: float) -> None: ...
    
    @abstractmethod
    async def swipe(self, udid: str, start_x: float, start_y: float, 
                    end_x: float, end_y: float, duration: float = 0.5) -> None: ...
    
    @abstractmethod
    async def type_text(self, udid: str, text: str) -> None: ...
    
    @abstractmethod
    async def press_button(self, udid: str, button: str) -> None: ...
    
    @abstractmethod
    async def set_location(self, udid: str, lat: float, lon: float) -> None: ...
    
    @abstractmethod
    async def grant_permission(self, udid: str, bundle_id: str, 
                               permission: str) -> None: ...
```

### 4.2 DeviceController

```python
class DeviceController:
    """Orchestrates device operations across backends."""
    
    def __init__(self):
        self._simctl = SimctlBackend()
        self._idb = IdbBackend()
        self._active_device: str | None = None
    
    async def check_tools(self) -> dict[str, bool]:
        """Check which tools are available."""
        return {
            "simctl": await self._simctl.is_available(),
            "idb": await self._idb.is_available(),
            "devicectl": False,  # Phase 3d
        }
    
    def _get_backend(self, operation: str) -> DeviceBackend:
        """Route operation to appropriate backend."""
        # Device management → simctl (simulators) or devicectl (physical)
        # UI automation → idb (simulators) or WDA (physical, Phase 3d)
        ...
```

### 4.3 SimctlBackend

Handles device and app management via `xcrun simctl`:

```python
class SimctlBackend:
    """Device management via xcrun simctl."""
    
    async def _run(self, *args: str) -> subprocess.CompletedProcess:
        """Execute a simctl command."""
        cmd = ["xcrun", "simctl"] + list(args)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise DeviceError(f"simctl {args[0]} failed: {stderr.decode()}")
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    
    async def list_devices(self) -> list[DeviceInfo]:
        result = await self._run("list", "devices", "--json")
        # Parse JSON output → list of DeviceInfo
        ...
    
    async def boot(self, udid: str) -> None:
        await self._run("boot", udid)
    
    async def screenshot(self, udid: str) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            await self._run("io", udid, "screenshot", f.name)
            return Path(f.name).read_bytes()
    
    async def install_app(self, udid: str, app_path: str) -> None:
        await self._run("install", udid, app_path)
    
    async def launch_app(self, udid: str, bundle_id: str) -> None:
        await self._run("launch", udid, bundle_id)
    
    async def set_location(self, udid: str, lat: float, lon: float) -> None:
        await self._run("location", udid, "set", str(lat), str(lon))
    
    async def grant_permission(self, udid: str, bundle_id: str, 
                               permission: str) -> None:
        await self._run("privacy", udid, "grant", permission, bundle_id)
```

### 4.4 IdbBackend

Handles UI automation via `idb`:

```python
class IdbBackend:
    """UI automation via Facebook idb."""
    
    async def _run(self, *args: str, udid: str) -> subprocess.CompletedProcess:
        """Execute an idb command."""
        cmd = ["idb"] + list(args) + ["--udid", udid]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise DeviceError(f"idb {args[0]} failed: {stderr.decode()}")
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    
    async def get_ui_tree(self, udid: str) -> UITree:
        result = await self._run("ui", "describe-all", udid=udid)
        # Parse idb's accessibility tree output → UITree
        ...
    
    async def tap(self, udid: str, x: float, y: float) -> None:
        await self._run("ui", "tap", str(x), str(y), udid=udid)
    
    async def swipe(self, udid: str, start_x: float, start_y: float,
                    end_x: float, end_y: float, duration: float = 0.5) -> None:
        await self._run("ui", "swipe", 
                        str(start_x), str(start_y),
                        str(end_x), str(end_y),
                        str(duration), udid=udid)
    
    async def type_text(self, udid: str, text: str) -> None:
        await self._run("ui", "text", text, udid=udid)
    
    async def press_button(self, udid: str, button: str) -> None:
        # idb supports: HOME, LOCK, SIDE_BUTTON, SIRI
        await self._run("ui", "key", button, udid=udid)
```

---

## 5. Data Models

```python
class DeviceType(str, Enum):
    SIMULATOR = "simulator"
    DEVICE = "device"

class DeviceState(str, Enum):
    BOOTED = "booted"
    SHUTDOWN = "shutdown"
    BOOTING = "booting"

class DeviceInfo(BaseModel):
    udid: str
    name: str                       # "iPhone 16 Pro"
    state: DeviceState
    device_type: DeviceType
    os_version: str                 # "iOS 18.2"
    runtime: str | None = None      # "com.apple.CoreSimulator.SimRuntime.iOS-18-2"
    is_available: bool = True

class AppInfo(BaseModel):
    bundle_id: str                  # "com.example.MyApp"
    name: str                       # "MyApp"
    app_type: str                   # "user", "system"
    architecture: str | None = None
    install_type: str | None = None # "unknown", "system", "user"
    process_state: str | None = None

class UIElement(BaseModel):
    """Single node in the accessibility tree."""
    type: str                       # "Button", "StaticText", "TextField", etc.
    label: str | None = None        # Accessibility label
    identifier: str | None = None   # Accessibility identifier
    value: str | None = None        # Current value (for text fields, switches, etc.)
    frame: dict | None = None       # {"x": 0, "y": 0, "width": 100, "height": 44}
    enabled: bool = True
    visible: bool = True
    traits: list[str] = []          # ["button", "staticText", etc.]
    children: list["UIElement"] = []

class UITree(BaseModel):
    """Full accessibility tree for a screen."""
    app_bundle_id: str | None = None
    root: UIElement
    element_count: int
    timestamp: datetime

    def flatten(self) -> list[UIElement]:
        """Return all elements as a flat list with computed center coordinates."""
        ...
    
    def find_by_label(self, label: str) -> list[UIElement]:
        """Find elements by accessibility label."""
        ...
    
    def find_by_type(self, element_type: str) -> list[UIElement]:
        """Find elements by type (Button, TextField, etc.)."""
        ...

class TapRequest(BaseModel):
    x: float
    y: float
    udid: str | None = None         # defaults to active device

class TapElementRequest(BaseModel):
    label: str | None = None        # tap by accessibility label
    identifier: str | None = None   # tap by accessibility identifier
    element_type: str | None = None # narrow search: "Button", "TextField"
    udid: str | None = None

class SwipeRequest(BaseModel):
    start_x: float
    start_y: float
    end_x: float
    end_y: float
    duration: float = 0.5           # seconds
    udid: str | None = None

class TypeTextRequest(BaseModel):
    text: str
    udid: str | None = None

class PressButtonRequest(BaseModel):
    button: str                     # HOME, LOCK, SIDE_BUTTON, SIRI
    udid: str | None = None

class SetLocationRequest(BaseModel):
    latitude: float
    longitude: float
    udid: str | None = None

class GrantPermissionRequest(BaseModel):
    bundle_id: str
    permission: str                 # location, camera, photos, microphone, contacts, etc.
    udid: str | None = None

class InstallAppRequest(BaseModel):
    app_path: str                   # path to .app or .ipa
    udid: str | None = None

class LaunchAppRequest(BaseModel):
    bundle_id: str
    udid: str | None = None
```

---

## 6. The tap-element Workflow

The `tap-element` endpoint is a convenience that combines tree inspection + tap. Rather than making the AI agent call `get_ui_tree`, parse the response, find the element's coordinates, then call `tap`, we do it server-side:

```python
async def tap_element(request: TapElementRequest, controller: DeviceController):
    """Find an element by label/identifier and tap its center."""
    udid = request.udid or controller.active_device
    
    # 1. Get accessibility tree
    tree = await controller.get_ui_tree(udid)
    
    # 2. Find matching element
    matches = []
    if request.label:
        matches = tree.find_by_label(request.label)
    elif request.identifier:
        matches = tree.find_by_identifier(request.identifier)
    
    # 3. Narrow by type if specified
    if request.element_type and matches:
        matches = [m for m in matches if m.type == request.element_type]
    
    if not matches:
        raise HTTPException(404, f"No element found matching criteria")
    
    if len(matches) > 1:
        # Return all matches so the AI can refine
        return {
            "status": "ambiguous",
            "matches": [{"label": m.label, "type": m.type, "frame": m.frame} 
                       for m in matches],
            "message": f"Found {len(matches)} matches. Specify element_type or use coordinates."
        }
    
    # 4. Calculate center and tap
    element = matches[0]
    center_x = element.frame["x"] + element.frame["width"] / 2
    center_y = element.frame["y"] + element.frame["height"] / 2
    await controller.tap(udid, center_x, center_y)
    
    return {"status": "ok", "tapped": {"label": element.label, "x": center_x, "y": center_y}}
```

This is a significant UX improvement for the AI agent — one tool call instead of three.

---

## 7. Screenshot Handling

Screenshots are the AI agent's "eyes." They need to be efficient and useful.

### 7.1 Capture

```
GET /api/v1/device/screenshot?udid=<udid>&format=png&scale=0.5
```

Parameters:
- `format`: `png` (default) or `jpeg`
- `scale`: Downscale factor (0.25, 0.5, 1.0). Default 0.5 for token efficiency.
- `quality`: JPEG quality 1-100 (default 80, only for JPEG)

Response: Binary image with appropriate Content-Type.

### 7.2 Annotated Screenshots

An optional endpoint that overlays the accessibility tree onto the screenshot:

```
GET /api/v1/device/screenshot/annotated?udid=<udid>
```

Draws bounding boxes around interactive elements with their labels. This gives the AI agent a visual + structural understanding in one image.

### 7.3 LLM-Optimized Screen Description

```
GET /api/v1/device/screen-summary?udid=<udid>
```

Returns a text description of the current screen, combining the accessibility tree with element counts and interactive element highlights:

```json
{
  "app": "com.example.geocaching",
  "screen_description": "Map view with search bar at top. 3 visible geocache pins. Bottom tab bar with 5 tabs (Map selected). Navigation bar shows 'Nearby Caches' title.",
  "interactive_elements": [
    {"type": "TextField", "label": "Search", "frame": {"x": 16, "y": 60, "w": 343, "h": 36}},
    {"type": "Button", "label": "Filter", "frame": {"x": 335, "y": 64, "w": 28, "h": 28}},
    {"type": "Button", "label": "GCAZ2TD", "frame": {"x": 180, "y": 340, "w": 40, "h": 40}}
  ],
  "element_counts": {"Button": 12, "StaticText": 8, "TextField": 1, "Image": 5},
  "timestamp": "2026-02-10T14:28:00Z"
}
```

This is much cheaper in tokens than a full screenshot or a raw accessibility tree dump.

---

## 8. HTTP API Design

### 8.1 Device Management

```
GET /api/v1/device/list
```

List all simulators and their states. Optionally filter by state.

Query params: `state` (booted/shutdown), `type` (simulator/device)

```json
{
  "devices": [
    {
      "udid": "9FED67A2-3D0A-4C9C-88AC-28A9CCA44C60",
      "name": "iPhone 16 Pro",
      "state": "booted",
      "device_type": "simulator",
      "os_version": "iOS 18.2",
      "is_available": true
    }
  ],
  "total": 1,
  "tools": {"simctl": true, "idb": true, "devicectl": false}
}
```

```
POST /api/v1/device/boot
Body: {"udid": "..."} or {"name": "iPhone 16 Pro"}
```

Boot a simulator by UDID or name. Returns device info.

```
POST /api/v1/device/shutdown
Body: {"udid": "..."}
```

### 8.2 App Management

```
POST /api/v1/device/app/install
Body: {"app_path": "/path/to/MyApp.app", "udid": "..."}
```

```
POST /api/v1/device/app/launch
Body: {"bundle_id": "com.example.MyApp", "udid": "..."}
```

```
POST /api/v1/device/app/terminate
Body: {"bundle_id": "com.example.MyApp", "udid": "..."}
```

```
GET /api/v1/device/app/list?udid=...
```

### 8.3 Inspection

```
GET /api/v1/device/screenshot?udid=...&format=png&scale=0.5
```

Returns image binary.

```
GET /api/v1/device/screenshot/annotated?udid=...
```

Returns screenshot with accessibility bounding boxes overlaid.

```
GET /api/v1/device/ui?udid=...
```

Returns the full accessibility tree as JSON.

```
GET /api/v1/device/screen-summary?udid=...
```

Returns LLM-optimized screen description (structured text, not an image).

### 8.4 Interaction

```
POST /api/v1/device/ui/tap
Body: {"x": 200, "y": 400, "udid": "..."}
```

```
POST /api/v1/device/ui/tap-element
Body: {"label": "Log In", "element_type": "Button", "udid": "..."}
```

Finds element in accessibility tree by label/identifier and taps its center. Returns 404 if not found, returns multiple matches if ambiguous.

```
POST /api/v1/device/ui/swipe
Body: {"start_x": 200, "start_y": 600, "end_x": 200, "end_y": 200, "duration": 0.5}
```

```
POST /api/v1/device/ui/type
Body: {"text": "hello world"}
```

Types text into the currently focused field.

```
POST /api/v1/device/ui/press
Body: {"button": "HOME"}
```

Press a hardware button (HOME, LOCK, SIDE_BUTTON, SIRI).

### 8.5 Device Configuration

```
POST /api/v1/device/location
Body: {"latitude": 47.6062, "longitude": -122.3321}
```

Set simulated GPS location.

```
POST /api/v1/device/permission
Body: {"bundle_id": "com.example.MyApp", "permission": "location", "udid": "..."}
```

Grant an app permission (location, camera, photos, microphone, contacts, calendar, reminders, health, homekit, siri, speech).

---

## 9. MCP Tools

| Tool | Description | Maps To |
|------|-------------|---------|
| `list_devices` | List simulators/devices and their states | GET /device/list |
| `boot_device` | Boot a simulator | POST /device/boot |
| `shutdown_device` | Shutdown a simulator | POST /device/shutdown |
| `install_app` | Install an app | POST /device/app/install |
| `launch_app` | Launch an app | POST /device/app/launch |
| `terminate_app` | Terminate an app | POST /device/app/terminate |
| `list_apps` | List installed apps | GET /device/app/list |
| `take_screenshot` | Capture screenshot (returns base64) | GET /device/screenshot |
| `get_ui_tree` | Get accessibility tree | GET /device/ui |
| `get_screen_summary` | LLM-optimized screen description | GET /device/screen-summary |
| `tap` | Tap at coordinates | POST /device/ui/tap |
| `tap_element` | Tap element by label/identifier | POST /device/ui/tap-element |
| `swipe` | Swipe gesture | POST /device/ui/swipe |
| `type_text` | Type text into focused field | POST /device/ui/type |
| `press_button` | Press hardware button | POST /device/ui/press |
| `set_location` | Set GPS coordinates | POST /device/location |
| `grant_permission` | Grant app permission | POST /device/permission |

### MCP Resources

- `logserver://guide` — update with device control workflows
- `logserver://troubleshooting` — add idb troubleshooting (empty tree, companion not running)

### Recommended AI Agent Workflow

```
1. list_devices → find booted simulator
2. launch_app → start the app
3. get_screen_summary → understand current screen
4. tap_element(label="Search") → interact
5. type_text("geocache name") → enter text
6. get_screen_summary → verify result
7. get_flow_summary → check what network requests happened
8. tail_logs → check for errors
```

---

## 10. Active Device Concept

To reduce boilerplate, the server tracks an "active device" — the most recently booted or interacted-with simulator. All endpoints accept an optional `udid` parameter; when omitted, the active device is used.

```python
class DeviceController:
    def __init__(self):
        self._active_udid: str | None = None
    
    async def resolve_udid(self, udid: str | None = None) -> str:
        """Resolve UDID: explicit > active > only booted > error."""
        if udid:
            self._active_udid = udid
            return udid
        if self._active_udid:
            return self._active_udid
        # Auto-detect: if exactly one simulator is booted, use it
        devices = await self.list_devices()
        booted = [d for d in devices if d.state == DeviceState.BOOTED]
        if len(booted) == 1:
            self._active_udid = booted[0].udid
            return self._active_udid
        if len(booted) == 0:
            raise DeviceError("No booted simulator found. Boot one first.")
        raise DeviceError(
            f"{len(booted)} simulators booted. Specify udid explicitly."
        )
```

---

## 11. Tool Availability & Graceful Degradation

Not all tools may be installed. The server should degrade gracefully:

| Tool Missing | Impact | Behavior |
|---|---|---|
| `simctl` not found | Can't manage simulators | Very unlikely (comes with Xcode). Error at startup. |
| `idb` not found | Can't do UI automation | Screenshot + device management still work. UI endpoints return 503 with install instructions. |
| `idb_companion` not running | idb commands fail | Auto-detect and report in `/device/list` response. |

The `GET /device/list` response includes a `tools` object showing what's available, so the AI agent knows its capabilities.

---

## 12. Dependencies

### Required
- macOS with Xcode (provides `simctl`, `devicectl`)

### Optional (but needed for UI automation)
- `idb_companion`: `brew tap facebook/fb && brew install idb-companion`
- `fb-idb`: `pip install fb-idb` (Python 3.11+ recommended, avoid 3.14)

### Python packages
- Pillow (for screenshot manipulation, annotated screenshots, scaling)

Add to pyproject.toml:
```
"Pillow>=10.0",
```

---

## 13. Project Structure (new files)

```
server/
  device/                           # New: device control code
    __init__.py
    controller.py                   # DeviceController orchestrator
    simctl.py                       # SimctlBackend
    idb.py                          # IdbBackend
    ui_tree.py                      # Accessibility tree parsing + UITree model
    screenshots.py                  # Screenshot capture, scaling, annotation
  api/
    device.py                       # /device/* route handlers
  models.py                         # + Device/App/UI models

mcp/src/
  index.ts                          # + device control tools

tests/
  test_device_controller.py
  test_simctl_backend.py
  test_idb_backend.py
  test_ui_tree.py
  test_device_api.py
  fixtures/
    simctl_list_output.json         # Sample simctl list --json output
    idb_describe_all_output.txt     # Sample idb ui describe-all output
```

---

## 14. Implementation Phases

### Phase 3a: Device Management & Screenshots (Week 1)

1. Data models (DeviceInfo, AppInfo, etc.)
2. SimctlBackend (list, boot, shutdown, install, launch, terminate, screenshot)
3. DeviceController with active device tracking
4. API routes for device management + screenshot
5. Tool availability checking
6. Tests with mocked subprocess calls

**Milestone:** Boot a simulator, install an app, take a screenshot — all via API.

### Phase 3b: UI Inspection (Week 2)

1. IdbBackend (describe-all, tap, swipe, type, key)
2. Accessibility tree parser (idb output → UITree model)
3. `GET /device/ui` endpoint
4. `GET /device/screen-summary` endpoint
5. `tap-element` convenience endpoint
6. Tests with fixture data

**Milestone:** Read the accessibility tree and get a structured screen description via API.

### Phase 3c: UI Interaction & MCP (Week 3)

1. Tap, swipe, type, press endpoints
2. Location and permission endpoints
3. Annotated screenshot generation (Pillow overlay)
4. All MCP tools
5. Update MCP guide resource
6. Integration tests

**Milestone:** Full interaction workflow via MCP — launch app, read screen, tap button, verify result.

### Phase 3d: Physical Device Support (Week 4, deferred)

1. DevicectlBackend for device management
2. WDA integration for UI automation on physical devices
3. Unified DeviceController routing (simulator vs physical)
4. Setup guide for physical device configuration

---

## 15. Design Decisions

1. **Command dispatch, not long-running subprocess.** Unlike Phases 1-2, there's no persistent process to manage. Each operation is a subprocess call that completes immediately.

2. **simctl for management, idb for UI.** Clean separation. simctl is always available and handles the reliable operations. idb adds UI automation on top.

3. **Active device tracking.** Reduces boilerplate for the AI agent. Auto-resolves to the only booted simulator when unambiguous.

4. **tap-element as a convenience.** Server-side tree lookup + tap in one call. Returns "ambiguous" with candidates when multiple matches found, rather than failing.

5. **Screenshot scaling.** Default 0.5x to reduce token cost. Full resolution available when needed.

6. **LLM-optimized screen summary.** Text-based screen description is cheaper than screenshots and often more useful for the AI agent's reasoning.

7. **Graceful degradation.** Server works without idb (device management + screenshots still functional). Clear error messages when idb is needed but missing.

8. **Physical device support deferred.** WDA adds significant complexity (building, signing, deploying). Simulator-first proves the concept; physical device support follows the same API surface.
