# Quern REST API — Request Body Schemas

Quick reference for the most commonly used endpoints. All POST/PATCH endpoints accept JSON bodies.
GET endpoints use query parameters. Optional fields can be omitted.

## Table of Contents

- [Device UI](#device-ui)
- [Device App](#device-app)
- [Device App State / Plist](#device-app-state--plist)
- [Device Control](#device-control)
- [Device Resolution](#device-resolution)
- [Proxy Mocking](#proxy-mocking)
- [Proxy Capture](#proxy-capture)
- [Proxy Flows](#proxy-flows)
- [Proxy Intercept](#proxy-intercept)

---

## Device UI

### POST /api/v1/device/ui/tap-element
```json
{
  "label": "string",              // exact match
  "label_contains": "string",     // substring match
  "label_prefix": "string",       // prefix match
  "identifier": "string",         // accessibility identifier
  "element_type": "string",       // e.g. "button", "staticText"
  "udid": "string",               // optional device
  "skip_stability_check": false,  // default false
  "source_timeout": 10            // optional, seconds
}
```
At least one of `label`, `label_contains`, `label_prefix`, or `identifier` is required.

### POST /api/v1/device/ui/tap
```json
{
  "x": 100,       // required
  "y": 200,       // required
  "udid": "string"
}
```

### POST /api/v1/device/ui/type
```json
{
  "text": "hello world",  // required
  "udid": "string"
}
```

### POST /api/v1/device/ui/swipe
```json
{
  "start_x": 200,    // required
  "start_y": 400,    // required
  "end_x": 200,      // required
  "end_y": 100,      // required
  "duration": 0.5,   // default 0.5 seconds
  "udid": "string"
}
```

### POST /api/v1/device/ui/press
```json
{
  "button": "home",   // "home", "volumeUp", "volumeDown"
  "udid": "string"
}
```

### POST /api/v1/device/ui/wait-for-element
```json
{
  "label": "string",
  "label_contains": "string",
  "label_prefix": "string",
  "identifier": "string",
  "type": "string",            // element type filter
  "condition": "exists",       // required: exists|not_exists|visible|enabled|disabled|value_equals|value_contains
  "value": "string",          // for value_equals/value_contains conditions
  "timeout": 10,              // default 10 seconds
  "interval": 0.5,            // default 0.5 seconds polling interval
  "udid": "string"
}
```

### POST /api/v1/device/ui/clear
```json
{
  "udid": "string"
}
```

### GET /api/v1/device/ui
Query params: `udid`, `children_of`, `snapshot_depth`, `strategy`, `source_timeout`

### GET /api/v1/device/ui/element
Query params: `label`, `label_contains`, `label_prefix`, `identifier`, `type`, `udid`

### GET /api/v1/device/screen-summary
Query params: `max_elements`, `udid`, `snapshot_depth`, `strategy`, `source_timeout`

### GET /api/v1/device/screenshot
Query params: `udid`, `format`, `scale`, `quality`

### GET /api/v1/device/screenshot/annotated
Query params: `udid`, `scale`, `quality`

---

## Device App

### POST /api/v1/device/app/launch
```json
{
  "bundle_id": "com.example.app",  // required
  "udid": "string",
  "env": {"KEY": "VALUE"}          // environment variables
}
```

### POST /api/v1/device/app/terminate
```json
{
  "bundle_id": "com.example.app",  // required
  "udid": "string"
}
```

### POST /api/v1/device/app/install
```json
{
  "app_path": "/path/to/app.app",  // required
  "udid": "string"
}
```

### POST /api/v1/device/app/uninstall
```json
{
  "bundle_id": "com.example.app",  // required
  "udid": "string"
}
```

### GET /api/v1/device/app/list
Query params: `udid`

---

## Device App State / Plist

### GET /api/v1/device/app/state/plist
Query params (all required except `key`, `udid`): `bundle_id`, `container`, `plist_path`, `key`, `udid`

### POST /api/v1/device/app/state/plist
```json
{
  "bundle_id": "com.example.app",       // required
  "container": "data",                   // required: data|group
  "plist_path": "Library/Preferences/com.example.app.plist",  // required
  "key": "someFlag",                     // required
  "value": true,                         // required (any JSON type)
  "udid": "string"
}
```

### POST /api/v1/device/app/state/plist/batch
```json
{
  "bundle_id": "com.example.app",       // required
  "container": "data",                   // required
  "plist_path": "Library/Preferences/com.example.app.plist",  // required
  "values": {                            // required
    "flag1": true,
    "flag2": "value",
    "flag3": 42
  },
  "udid": "string"
}
```

### DELETE /api/v1/device/app/state/plist/key
Query params: `bundle_id` (required), `container` (required), `plist_path` (required), `key` (required), `udid`

### GET /api/v1/device/app/state/plist/diff
Query params: `bundle_id` (required), `container` (required), `plist_path` (required), `checkpoint_label` (required), `udid`

### POST /api/v1/device/app/state/save
```json
{
  "bundle_id": "com.example.app",  // required
  "label": "clean-login",          // required
  "description": "After first login",
  "udid": "string"
}
```

### POST /api/v1/device/app/state/restore
```json
{
  "bundle_id": "com.example.app",  // required
  "label": "clean-login",          // required
  "udid": "string"
}
```

### GET /api/v1/device/app/state/list
Query params: `bundle_id` (required)

### DELETE /api/v1/device/app/state/{label}
Path param: `label`. Query params: `bundle_id` (required)

---

## Device Control

### POST /api/v1/device/open-url
```json
{
  "url": "https://example.com",  // required
  "udid": "string"
}
```

### POST /api/v1/device/permission
```json
{
  "bundle_id": "com.example.app",  // required
  "permission": "location",        // required: location, photos, camera, microphone, contacts, etc.
  "udid": "string"
}
```

### POST /api/v1/device/location
```json
{
  "latitude": 47.6062,    // required
  "longitude": -122.3321, // required
  "udid": "string"
}
```

### POST /api/v1/device/build-and-install
```json
{
  "project_path": "/path/to/project",  // required
  "scheme": "MyScheme",
  "configuration": "Debug",            // default: Debug
  "udid": "string",
  "udids": ["udid1", "udid2"]         // for multi-device
}
```

### GET /api/v1/device/list
Query params: `state`, `device_type`, `name`, `os_version`, `device_family`, `cert_installed`, `include_disconnected`

---

## Device Resolution

### POST /api/v1/devices/resolve
Note the **plural** `/devices/`.
```json
{
  "udid": "string",
  "name": "iPhone 16",
  "os_version": "18.2",          // accepts "18.2" or "iOS 18.2"
  "device_family": "iPhone",
  "device_type": "simulator",    // default: simulator
  "auto_boot": true              // default: true
}
```

### POST /api/v1/devices/ensure
```json
{
  "count": 3,                   // required
  "name": "iPhone 16",
  "os_version": "18.2",
  "device_family": "iPhone",
  "device_type": "simulator",  // default: simulator
  "auto_boot": true            // default: true
}
```

---

## Proxy Mocking

### POST /api/v1/proxy/mocks
Both flat and nested body shapes are accepted:
```json
{
  "pattern": "~d example.com & ~u /v1/users",
  "status_code": 200,
  "body": "{\"users\": []}"
}
```
```json
{
  "pattern": "~d example.com & ~u /v1/users",
  "response": {
    "status_code": 200,
    "headers": {"Content-Type": "application/json"},
    "body": "{\"users\": []}"
  }
}
```

### GET /api/v1/proxy/mocks
No params. Returns list of active mock rules.

### DELETE /api/v1/proxy/mocks
No params. Clears all mock rules.

### PATCH /api/v1/proxy/mocks/{rule_id}
Path param: `rule_id`. Body: same shape as POST (fields to update).

---

## Proxy Capture

### POST /api/v1/proxy/capture/start
```json
{
  "id": "my-session",            // optional session ID
  "hosts": ["api.example.com"],  // filter to these hosts
  "exclude_hosts": ["analytics.example.com"],
  "simulator_udid": "string",    // filter by simulator
  "client_ip": "string",
  "detail": "full"               // default: full (full|summary|minimal)
}
```

### POST /api/v1/proxy/capture/stop
```json
{
  "session_id": "my-session"  // required — the ID from start
}
```

---

## Proxy Flows

### GET /api/v1/proxy/flows
Query params: `host`, `hosts`, `exclude_hosts`, `path_contains`, `method`, `status_min`, `status_max`, `has_error`, `since`, `until`, `device_id`, `simulator_udid`, `client_ip`, `detail`, `limit`, `offset`

### GET /api/v1/proxy/flows/summary
Query params: `window`, `host`, `since_cursor`, `simulator_udid`, `client_ip`

### GET /api/v1/proxy/flows/{flow_id}
Path param: `flow_id`

### POST /api/v1/proxy/flows/wait
```json
{
  "host": "api.example.com",
  "path_contains": "/v1/users",
  "method": "POST",
  "status_min": 200,
  "status_max": 299,
  "has_error": false,
  "simulator_udid": "string",
  "client_ip": "string",
  "timeout": 10,       // default 10 seconds
  "interval": 0.5,     // default 0.5 seconds
  "since": "ISO datetime"
}
```

---

## Proxy Intercept

### POST /api/v1/proxy/intercept
```json
{
  "pattern": "~d api.example.com & ~m POST"  // required, mitmproxy filter
}
```

### DELETE /api/v1/proxy/intercept
No body. Clears the intercept.

### GET /api/v1/proxy/intercept/held
Query params: `timeout` (seconds to wait for held flows)

### POST /api/v1/proxy/intercept/release
```json
{
  "flow_id": "abc123",       // required
  "modifications": {         // optional — modify the request/response before releasing
    "status_code": 200,
    "body": "{\"modified\": true}"
  }
}
```