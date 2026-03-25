---
name: quern-api
description: >
  Quern REST API reference — the correct HTTP endpoints, methods, and request body schemas
  for the Quern debug server at localhost:9100. Use this skill whenever writing Python scripts,
  curl/httpie commands, or any HTTP requests targeting the Quern REST API. Also use when the
  user asks about quern REST paths, endpoint mappings, or how to call quern from code. The MCP
  tool names do NOT match REST paths (e.g., type_text → /device/ui/type, not /device/ui/type-text),
  so this reference is essential to avoid guessing wrong. Trigger on: quern + (python|curl|requests|
  httpie|script|REST|API|endpoint|path), or when you see localhost:9100 in code being written.
---

# Quern REST API Reference

The Quern debug server exposes a REST API at `http://localhost:9100`. MCP tool names differ
from REST paths in non-obvious ways — this reference gives you the correct mappings.

## Critical Rules

1. **All paths start with `/api/v1/`** — never omit the prefix
2. **Hyphens, not underscores** in URL paths: `tap-element`, `open-url`, `wait-for-element`
3. **`type_text` is `/device/ui/type`** — NOT `/device/ui/type-text`
4. **`/devices/` (plural)** for resolve/ensure — **`/device/` (singular)** for everything else
5. **Mocks use `/proxy/mocks`** (plural) — not `/proxy/mock`
6. **Auth**: `X-API-Key` header — read the value from `~/.quern/state.json` field `api_key`
7. **Device targeting**: most endpoints accept `udid` as a query param (GET) or body field (POST)

## MCP Tool → REST Endpoint Mapping

### Device UI

| MCP Tool | Method | Path |
|----------|--------|------|
| `tap` | POST | `/api/v1/device/ui/tap` |
| `tap_element` | POST | `/api/v1/device/ui/tap-element` |
| `type_text` | POST | `/api/v1/device/ui/type` |
| `clear_text` | POST | `/api/v1/device/ui/clear` |
| `swipe` | POST | `/api/v1/device/ui/swipe` |
| `press_button` | POST | `/api/v1/device/ui/press` |
| `wait_for_element` | POST | `/api/v1/device/ui/wait-for-element` |
| `get_ui_tree` | GET | `/api/v1/device/ui` |
| `get_element_state` | GET | `/api/v1/device/ui/element` |
| `get_screen_summary` | GET | `/api/v1/device/screen-summary` |
| `take_screenshot` | GET | `/api/v1/device/screenshot` |
| `take_annotated_screenshot` | GET | `/api/v1/device/screenshot/annotated` |

### Device App

| MCP Tool | Method | Path |
|----------|--------|------|
| `install_app` | POST | `/api/v1/device/app/install` |
| `launch_app` | POST | `/api/v1/device/app/launch` |
| `terminate_app` | POST | `/api/v1/device/app/terminate` |
| `uninstall_app` | POST | `/api/v1/device/app/uninstall` |
| `list_apps` | GET | `/api/v1/device/app/list` |

### Device App State / Plist

| MCP Tool | Method | Path |
|----------|--------|------|
| `read_app_plist` | GET | `/api/v1/device/app/state/plist` |
| `set_app_plist_value` | POST | `/api/v1/device/app/state/plist` |
| `set_app_plist_values` | POST | `/api/v1/device/app/state/plist/batch` |
| `delete_app_plist_key` | DELETE | `/api/v1/device/app/state/plist/key` |
| `diff_app_plist` | GET | `/api/v1/device/app/state/plist/diff` |
| `save_app_state` | POST | `/api/v1/device/app/state/save` |
| `restore_app_state` | POST | `/api/v1/device/app/state/restore` |
| `list_app_states` | GET | `/api/v1/device/app/state/list` |
| `delete_app_state` | DELETE | `/api/v1/device/app/state/{label}` |
| `configure_plist_watch` | POST | `/api/v1/device/app/state/plist/watch/configure` |
| `unconfigure_plist_watch` | DELETE | `/api/v1/device/app/state/plist/watch/configure` |
| `get_plist_watch_config` | GET | `/api/v1/device/app/state/plist/watch/config` |
| `start_plist_watch` | POST | `/api/v1/device/app/state/plist/watch/start` |
| `stop_plist_watch` | POST | `/api/v1/device/app/state/plist/watch/stop` |

### Device Control

| MCP Tool | Method | Path |
|----------|--------|------|
| `open_url` | POST | `/api/v1/device/open-url` |
| `boot_device` | POST | `/api/v1/device/boot` |
| `shutdown_device` | POST | `/api/v1/device/shutdown` |
| `grant_permission` | POST | `/api/v1/device/permission` |
| `set_locale` | POST | `/api/v1/device/locale` |
| `set_location` | POST | `/api/v1/device/location` |
| `set_display_density` | POST | `/api/v1/device/display-density` |
| `set_font_scale` | POST | `/api/v1/device/font-scale` |
| `build_and_install` | POST | `/api/v1/device/build-and-install` |
| `list_devices` | GET | `/api/v1/device/list` |
| `preview_device` | POST | `/api/v1/device/preview/start` |
| `stop_preview` | POST | `/api/v1/device/preview/stop` |
| `preview_status` | GET | `/api/v1/device/preview/status` |
| `record_device_proxy_config` | POST | `/api/v1/proxy/device-proxy-config` |

### Device Drivers (WDA)

| MCP Tool | Method | Path |
|----------|--------|------|
| `setup_wda` | POST | `/api/v1/device/wda/setup` |
| `start_driver` | POST | `/api/v1/device/wda/start` |
| `stop_driver` | POST | `/api/v1/device/wda/stop` |

### Device Resolution (note: plural `/devices/`)

| MCP Tool | Method | Path |
|----------|--------|------|
| `resolve_device` | POST | `/api/v1/devices/resolve` |
| `ensure_devices` | POST | `/api/v1/devices/ensure` |

### Logging

| MCP Tool | Method | Path |
|----------|--------|------|
| `query_logs` | GET | `/api/v1/logs/query` |
| `get_log_summary` | GET | `/api/v1/logs/summary` |
| `get_errors` | GET | `/api/v1/logs/errors` |
| `list_log_sources` | GET | `/api/v1/logs/sources` |
| `get_log_filter` | GET | `/api/v1/logs/filter` |
| `set_log_filter` | POST | `/api/v1/logs/filter` |
| `start_oslog_streaming` | POST | `/api/v1/logs/oslog/start` |
| `stop_oslog_streaming` | POST | `/api/v1/logs/oslog/stop` |
| `start_simulator_logging` | POST | `/api/v1/device/logging/start` |
| `stop_simulator_logging` | POST | `/api/v1/device/logging/stop` |
| `start_device_logging` | POST | `/api/v1/device/logging/device/start` |
| `stop_device_logging` | POST | `/api/v1/device/logging/device/stop` |

### Proxy / Network

| MCP Tool | Method | Path |
|----------|--------|------|
| `proxy_status` | GET | `/api/v1/proxy/status` |
| `start_proxy` | POST | `/api/v1/proxy/start` |
| `stop_proxy` | POST | `/api/v1/proxy/stop` |
| `configure_system_proxy` | POST | `/api/v1/proxy/configure-system` |
| `unconfigure_system_proxy` | POST | `/api/v1/proxy/unconfigure-system` |
| `set_local_capture` | POST | `/api/v1/proxy/local-capture` |
| `query_flows` | GET | `/api/v1/proxy/flows` |
| `get_flow_summary` | GET | `/api/v1/proxy/flows/summary` |
| `get_flow_detail` | GET | `/api/v1/proxy/flows/{flow_id}` |
| `wait_for_flow` | POST | `/api/v1/proxy/flows/wait` |
| `replay_flow` | POST | `/api/v1/proxy/replay/{flow_id}` |

### Proxy Mocking (plural `/mocks`)

| MCP Tool | Method | Path |
|----------|--------|------|
| `set_mock` | POST | `/api/v1/proxy/mocks` |
| `list_mocks` | GET | `/api/v1/proxy/mocks` |
| `clear_mocks` | DELETE | `/api/v1/proxy/mocks` |
| `update_mock` | PATCH | `/api/v1/proxy/mocks/{rule_id}` |

### Proxy Intercept

| MCP Tool | Method | Path |
|----------|--------|------|
| `set_intercept` | POST | `/api/v1/proxy/intercept` |
| `clear_intercept` | DELETE | `/api/v1/proxy/intercept` |
| `list_held_flows` | GET | `/api/v1/proxy/intercept/held` |
| `release_flow` | POST | `/api/v1/proxy/intercept/release` |

### Proxy Bypass

| MCP Tool | Method | Path |
|----------|--------|------|
| `set_bypass` | POST | `/api/v1/proxy/bypass` |
| (get bypass) | GET | `/api/v1/proxy/bypass` |
| `clear_bypass` | DELETE | `/api/v1/proxy/bypass` |

### Proxy Certificates

| MCP Tool | Method | Path |
|----------|--------|------|
| `install_proxy_cert` | POST | `/api/v1/proxy/cert/install` |
| `verify_proxy_setup` | POST | `/api/v1/proxy/cert/verify` |
| `proxy_setup_guide` | GET | `/api/v1/proxy/setup-guide` |

### Capture Sessions

| MCP Tool | Method | Path |
|----------|--------|------|
| `start_capture_session` | POST | `/api/v1/proxy/capture/start` |
| `stop_capture_session` | POST | `/api/v1/proxy/capture/stop` |

### Builds / Crashes

| MCP Tool | Method | Path |
|----------|--------|------|
| `get_build_result` | GET | `/api/v1/builds/latest` |
| `parse_build_output` | POST | `/api/v1/builds/parse` |
| `get_latest_crash` | GET | `/api/v1/crashes/latest` |

### MCP-Only Tools (no REST equivalent)

These tools exist only in the MCP interface and have no REST endpoint:
- `ensure_server` — checks/restarts the quern server process
- `init_app_knowledge` — initializes app knowledge base from bundled data
- `tail_logs` — convenience wrapper; use `GET /api/v1/logs/query?tail=true` instead

## Request Body Schemas

For the full request/response schemas of any endpoint, fetch the OpenAPI spec:
```
curl http://localhost:9100/openapi.json
```

For commonly-used endpoint schemas, see `references/schemas.md`.

## Python Helper Pattern

```python
import json, requests

def quern(method, path, **kwargs):
    """Helper for calling the Quern REST API."""
    with open(os.path.expanduser("~/.quern/state.json")) as f:
        api_key = json.load(f)["api_key"]
    url = f"http://localhost:9100/api/v1{path}"
    headers = {"X-API-Key": api_key}
    if method.upper() == "GET":
        return requests.get(url, headers=headers, params=kwargs)
    return requests.request(method, url, headers=headers, json=kwargs)
```

## Mock Filter Syntax

Mocks use mitmproxy filter expressions. Key operators:
- `~d` (domain), `~u` (URL substring), `~m` (method)
- Combine with `&`: `"~d api.example.com & ~m POST & ~u /v1/login"`
- `~p` does NOT exist — use `~u` for path matching

```python
# Example: mock a POST endpoint
quern("POST", "/proxy/mocks",
      pattern="~d staging.api.groundspeak.com & ~u /v1/users",
      status_code=200, body='{"user": "test"}')
```