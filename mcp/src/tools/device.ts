import { writeFile, mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { discoverServer } from "../config.js";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerDeviceTools(server: McpServer): void {
  server.registerTool("list_devices", {
    description: `List available iOS and Android devices (simulators, emulators, physical), plus tool availability (simctl, idb, devicectl, adb). Returns device UDIDs, names, states, and OS versions. Does NOT change the active device.`,
    inputSchema: strictParams({
      state: z
        .enum(["booted", "shutdown"])
        .optional()
        .describe("Filter by device state"),
      type: z
        .enum(["simulator", "device", "android_emulator", "android_device"])
        .optional()
        .describe("Filter by device type"),
      name: z
        .string()
        .optional()
        .describe("Filter by device name (case-insensitive, exact match preferred, substring fallback)"),
      os_version: z
        .string()
        .optional()
        .describe("Filter by OS version prefix (e.g. '18', '18.2', 'iOS 18.2')"),
      device_family: z
        .string()
        .optional()
        .describe("Filter by device family: 'iPhone', 'iPad', 'Apple Watch', 'Apple TV'"),
      cert_installed: z
        .coerce.boolean()
        .optional()
        .describe("Filter by mitmproxy CA certificate installation status (true = cert installed, false = not installed)"),
      include_disconnected: z
        .coerce.boolean()
        .optional()
        .default(false)
        .describe(
          "Include physical devices that are paired but not currently reachable. By default, only connected devices are shown."
        ),
    }),
  }, async ({ state, type, name, os_version, device_family, cert_installed, include_disconnected }) => {
    try {
      const params: Record<string, string | number | boolean | undefined> = {};
      if (state) params.state = state;
      if (type) params.device_type = type;
      if (name) params.name = name;
      if (os_version) params.os_version = os_version;
      if (device_family) params.device_family = device_family;
      if (cert_installed !== undefined) params.cert_installed = cert_installed;
      if (include_disconnected) params.include_disconnected = true;

      const data = (await apiRequest("GET", "/api/v1/device/list", params)) as {
        devices: Array<Record<string, unknown>>;
        tools: Record<string, boolean>;
        active_udid: string | null;
      };

      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify(data, null, 2),
          },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("boot_device", {
    description: `Boot an iOS simulator or Android emulator by UDID or name. Not supported for physical devices.`,
    inputSchema: strictParams({
      udid: z.string().optional().describe("Device UDID to boot"),
      name: z
        .string()
        .optional()
        .describe('Device name to boot (e.g. "iPhone 16 Pro")'),
    }),
  }, async ({ udid, name }) => {
    try {
      const body: Record<string, unknown> = {};
      if (udid) body.udid = udid;
      if (name) body.name = name;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/boot",
        undefined,
        body
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("shutdown_device", {
    description: `Shutdown an iOS simulator or Android emulator. Not supported for physical devices.`,
    inputSchema: strictParams({
      udid: z.string().describe("Device UDID to shutdown"),
    }),
  }, async ({ udid }) => {
    try {
      const data = await apiRequest(
        "POST",
        "/api/v1/device/shutdown",
        undefined,
        { udid }
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("erase_device", {
    description: `Erase a simulator or Android emulator, resetting it to factory state. All apps, data, and settings are removed. A simulator is shut down first and left shut down. An Android emulator is killed and booted again with -wipe-data, so it comes back running, cold-booted, and possibly on a different serial — use the udid in the response, which also carries restarted: true (and previous_udid if the serial changed). A shut-down AVD (avd:NAME) is booted with -wipe-data and comes back running. The call returns once Android has finished starting, which can take a few minutes. If it fails after the emulator was shut down, the error says so: the emulator is gone and may already be wiped. Not supported for physical iOS or Android devices, or for an emulator attached over TCP.`,
    inputSchema: strictParams({
      udid: z.string().describe("Simulator UDID, Android emulator serial (emulator-NNNN), or a shut-down AVD as avd:NAME"),
    }),
  }, async ({ udid }) => {
    try {
      const data = await apiRequest(
        "POST",
        "/api/v1/device/erase",
        undefined,
        { udid }
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("install_app", {
    description: `Install an app (.app, .ipa, or .apk) on an iOS or Android device.`,
    inputSchema: strictParams({
      app_path: z.string().describe("Path to the .app, .ipa, or .apk file"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ app_path, udid }) => {
    try {
      const body: Record<string, unknown> = { app_path };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/app/install",
        undefined,
        body
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("launch_app", {
    description: `Launch an app by bundle ID (iOS) or package name (Android) on any device.

NOTE: If you want to capture network traffic from this app:
1. Ensure the proxy is running (start_proxy)
2. Enable system proxy (configure_system_proxy)
3. Launch the app (this tool)
4. When done, disable system proxy (unconfigure_system_proxy)`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle ID (iOS) or package name (Android), e.g. com.example.MyApp"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      env: z
        .record(z.string(), z.string())
        .optional()
        .describe("Environment variables for the app process, on an iOS simulator (simctl's SIMCTL_CHILD_ convention) or a physical iPhone (through WDA). Variables reach only a process that is starting, so with a non-empty env a running app is restarted; the response says so (restarted: true/false, or null when quern could not tell) and confirms env_applied. QUERN_AUTOMATION=YES is set whenever quern starts the app, unless env sets it. On a physical iPhone the launch is confirmed by the app reaching the foreground; launch_confirmed is null, with launch_check_error, when that could not be read. Android apps take no environment variables: env_applied is false there, with a warning. Names must be non-empty and contain no '=' or NUL."),
      include_screen_context: z
        .boolean()
        .default(false)
        .describe("Include a screen summary in the response after the app launches. Waits 0.5s for the screen to settle. With landmarks loaded it also tries to identify the screen you landed on, so you do not need a follow-up get_screen_summary?identify=true: confidence is 'exact', 'ambiguous' (candidates lists them) or 'none', and identified_as is null when nothing matched. Nothing is added when no landmarks are loaded. The summary carries \"backend\", naming which of quern's UI backends read the screen ('sim-bridge' or 'idb' on a simulator, 'wda' on a physical iPhone, 'u2' on Android) -- worth checking if the screen you landed on is not the one you expected."),
      capture_screenshots: z
        .boolean()
        .default(false)
        .describe("Capture before/after screenshots around the app launch."),
      settle_delay: z
        .coerce.number()
        .min(0)
        .max(10)
        .optional()
        .describe("Seconds to wait before capturing after screenshot/screen context (default 1.0)."),
    }),
  }, async ({ bundle_id, udid, env, include_screen_context, capture_screenshots, settle_delay }) => {
    try {
      const body: Record<string, unknown> = { bundle_id };
      if (udid) body.udid = udid;
      if (env) body.env = env;
      if (include_screen_context) body.include_screen_context = true;
      if (capture_screenshots) body.capture_screenshots = true;
      if (settle_delay !== undefined) body.settle_delay = settle_delay;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/app/launch",
        undefined,
        body
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("terminate_app", {
    description: `Terminate a running app by bundle ID (iOS) or package name (Android).`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle ID or package name"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/app/terminate",
        undefined,
        body
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("uninstall_app", {
    description: `Uninstall an app from an iOS or Android device by bundle ID or package name.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle ID or package name"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/app/uninstall",
        undefined,
        body
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("list_apps", {
    description: `List installed apps on an iOS or Android device.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ udid }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/app/list", {
        udid,
      });

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("take_screenshot", {
    description: `Capture a screenshot from a simulator or physical device. Returns the image as base64-encoded data, or saves to disk when save_path is provided.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      format: z
        .enum(["png", "jpeg"])
        .default("png")
        .describe("Image format"),
      scale: z
        .coerce.number()
        .min(0.1)
        .max(1.0)
        .default(0.5)
        .describe("Scale factor (0.1-1.0, default 0.5)"),
      quality: z
        .coerce.number()
        .min(1)
        .max(100)
        .default(85)
        .describe("JPEG quality (1-100, ignored for PNG)"),
      save_path: z
        .string()
        .optional()
        .describe(
          "Save screenshot to this file path instead of returning base64. Parent directories are created automatically."
        ),
    }),
  }, async ({ udid, format, scale, quality, save_path }) => {
    try {
      const srv = discoverServer();
      const url = new URL("/api/v1/device/screenshot", srv.url);
      if (udid) url.searchParams.set("udid", udid);
      url.searchParams.set("format", format);
      url.searchParams.set("scale", String(scale));
      url.searchParams.set("quality", String(quality));

      const resp = await fetch(url.toString(), {
        headers: { Authorization: `Bearer ${srv.apiKey}` },
      });

      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`HTTP ${resp.status}: ${text}`);
      }

      const buffer = Buffer.from(await resp.arrayBuffer());

      if (save_path) {
        await mkdir(dirname(save_path), { recursive: true });
        await writeFile(save_path, buffer);
        return {
          content: [
            {
              type: "text" as const,
              text: `Screenshot saved to ${save_path}`,
            },
          ],
        };
      }

      return {
        content: [
          {
            type: "image" as const,
            data: buffer.toString("base64"),
            mimeType:
              resp.headers.get("content-type") || "image/png",
          },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("take_annotated_screenshot", {
    description: `Capture a screenshot with accessibility annotations overlaid. Draws red bounding boxes and labels on interactive UI elements. When no interactive elements are found, automatically overlays a coordinate grid (in points, matching the tap coordinate system) so you can identify tap positions visually. Use grid=true to force the grid even when elements exist, or grid=<number> for custom point spacing.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      scale: z
        .coerce.number()
        .min(0.1)
        .max(1.0)
        .default(0.5)
        .describe("Scale factor (0.1-1.0, default 0.5)"),
      quality: z
        .coerce.number()
        .min(1)
        .max(100)
        .default(85)
        .describe("JPEG quality (1-100, used for base screenshot before annotation)"),
      grid: z
        .union([z.literal(true), z.coerce.number().int().min(0)])
        .optional()
        .describe(
          "Coordinate grid overlay. true = 50pt grid, number = custom spacing in points, 0 = disable auto-grid. Omit for auto (grid when no interactive elements found)."
        ),
      save_path: z
        .string()
        .optional()
        .describe(
          "Save screenshot to this file path instead of returning base64. Parent directories are created automatically."
        ),
    }),
  }, async ({ udid, scale, quality, grid, save_path }) => {
    try {
      const srv = discoverServer();
      const url = new URL("/api/v1/device/screenshot/annotated", srv.url);
      if (udid) url.searchParams.set("udid", udid);
      url.searchParams.set("scale", String(scale));
      url.searchParams.set("quality", String(quality));
      if (grid !== undefined) {
        const gridVal = grid === true ? 50 : grid;
        url.searchParams.set("grid", String(gridVal));
      }

      const resp = await fetch(url.toString(), {
        headers: { Authorization: `Bearer ${srv.apiKey}` },
      });

      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`HTTP ${resp.status}: ${text}`);
      }

      const buffer = Buffer.from(await resp.arrayBuffer());

      if (save_path) {
        await mkdir(dirname(save_path), { recursive: true });
        await writeFile(save_path, buffer);
        return {
          content: [
            {
              type: "text" as const,
              text: `Annotated screenshot saved to ${save_path}`,
            },
          ],
        };
      }

      return {
        content: [
          {
            type: "image" as const,
            data: buffer.toString("base64"),
            mimeType: "image/png",
          },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("start_screenshot_timeline", {
    description: `Start a screenshot timeline that auto-captures a screenshot after every UI action (tap, type, swipe, launch, open_url, etc.). Screenshots are saved sequentially with high-fidelity action labels. Call stop_screenshot_timeline to get the manifest with all entries.

Use this to build visual test reports — every action becomes a timestamped step with a screenshot.`,
    inputSchema: strictParams({
      udid: z.string().optional().describe("Device UDID for screenshots (defaults to active device)"),
      session_id: z.string().optional().describe("Custom session ID (auto-generated if omitted)"),
    }),
  }, async ({ udid, session_id }) => {
    try {
      const body: Record<string, unknown> = {};
      if (udid) body.udid = udid;
      if (session_id) body.session_id = session_id;
      const data = await apiRequest("POST", "/api/v1/device/screenshot/timeline/start", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("stop_screenshot_timeline", {
    description: `Stop the active screenshot timeline and return its manifest. The manifest lists every action with timestamp, action label, screenshot path, and HTTP status code.`,
    inputSchema: strictParams({}),
  }, async () => {
    try {
      const data = await apiRequest("POST", "/api/v1/device/screenshot/timeline/stop");
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("get_screenshot_timeline", {
    description: `Get the manifest of the active screenshot timeline without stopping it. Shows all entries captured so far.`,
    inputSchema: strictParams({}),
  }, async () => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/screenshot/timeline");
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("set_location", {
    description: `Set the simulated GPS location on an iOS simulator or Android emulator.`,
    inputSchema: strictParams({
      latitude: z.coerce.number().describe("GPS latitude"),
      longitude: z.coerce.number().describe("GPS longitude"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ latitude, longitude, udid }) => {
    try {
      const body: Record<string, unknown> = { latitude, longitude };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/location",
        undefined,
        body
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("open_url", {
    description: `Open a URL on a simulator, emulator, or physical iPhone using the platform's default handler. Supports any URI scheme the device has a handler for: https://, geo: (maps), tel:, mailto:, custom app URL schemes, deep links, universal links, and settings URIs (Android: android.settings.* actions, iOS: App-prefs:).

Note: tel: and mailto: are unavailable on iOS simulators (no Phone or Mail app).

Examples:
- Web: "https://example.com"
- Maps: "geo:48.8584,2.2945?z=15" (Android) or "maps://?ll=48.8584,2.2945&z=15" (iOS)
- Settings: "App-prefs:WIFI" (iOS) — Android uses action-based intents via adb
- Deep link: "myapp://path/to/screen"

Deep links open the way a tapped link does, on every platform: through the system's own routing, so an https link reaches the app only if the domain says it may -- apple-app-site-association on iOS (simctl openurl on a simulator, whichever UI backend is reading it; WDA's /url on a physical iPhone, iOS 16.4+), assetlinks.json App Links on Android (a VIEW intent with no package, carrying the BROWSABLE category a browser tap does for an http(s) link; other schemes go without it, as before). That is the route that tests what a user gets.

direct=true (Android only) delivers the intent to bundle_id instead, bypassing App Links verification the way the app's Espresso tests do. Use it for staging and other debug builds, whose links are not verified App Links and would otherwise open in the browser. iOS refuses direct: it has no route that keeps universal-link routing.

bundle_id names the app the link should open in, and quern reports whether it did: opened_in_app true/false with foreground_app (a bundle id or package on a device, the display name on a simulator) and on Android foreground_activity, plus a warning when it went elsewhere -- the browser, or Android's app chooser (more than one activity claims the URL). On Android, crashed says whether the app crashed after the open, read from the crash buffer, because a crash can leave the app's own previous screen in front and look like success. opened_in_app and crashed are null, with opened_in_app_error or crash_check_error, when quern could not tell. A sighting counts only 2s after the open, so a confirmed open takes at least that long. 'via' names the transport (simctl, wda, adb) and 'route' the routing (system, direct).`,
    inputSchema: strictParams({
      url: z.string().describe(
        "URL or URI to open (e.g. https://example.com, geo:48.8,2.3?z=15, maps://?ll=48.8,2.3)"
      ),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      bundle_id: z
        .string()
        .optional()
        .describe("The app the link should open in. quern waits up to 5s for it to come to the front and reports opened_in_app. It does not change how the URL is delivered unless direct=true."),
      direct: z
        .boolean()
        .optional()
        .describe("Android only: deliver the intent to bundle_id instead of the system's routing, bypassing App Links verification (as Espresso does). Needed for staging/debug builds, whose links are not verified App Links. Requires bundle_id; refused on iOS."),
      include_screen_context: z
        .boolean()
        .default(false)
        .describe("Include a screen summary in the response after the URL is handled. Waits 0.5s for the screen to settle. With landmarks loaded it also tries to identify the screen you landed on, so you do not need a follow-up get_screen_summary?identify=true: confidence is 'exact', 'ambiguous' (candidates lists them) or 'none', and identified_as is null when nothing matched. Nothing is added when no landmarks are loaded. The summary carries \"backend\", naming which of quern's UI backends read the screen ('sim-bridge' or 'idb' on a simulator, 'wda' on a physical iPhone, 'u2' on Android) -- worth checking if the screen you landed on is not the one you expected."),
      capture_screenshots: z
        .boolean()
        .default(false)
        .describe("Capture before/after screenshots around the URL open."),
      settle_delay: z
        .coerce.number()
        .min(0)
        .max(10)
        .optional()
        .describe("Seconds to wait before capturing after screenshot/screen context (default 1.0)."),
    }),
  }, async ({ url, udid, bundle_id, direct, include_screen_context, capture_screenshots, settle_delay }) => {
    try {
      const body: Record<string, unknown> = { url };
      if (udid) body.udid = udid;
      if (bundle_id) body.bundle_id = bundle_id;
      if (direct) body.direct = true;
      if (include_screen_context) body.include_screen_context = true;
      if (capture_screenshots) body.capture_screenshots = true;
      if (settle_delay !== undefined) body.settle_delay = settle_delay;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/open-url",
        undefined,
        body
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("grant_permission", {
    description: `Grant an app permission on an iOS simulator or Android device (emulator or physical). iOS permissions: photos, camera, location, contacts, calendar, microphone, notifications. Android also supports: storage, phone, sms, call-log, body-sensors, nearby-devices, or any full android.permission.* string.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      permission: z
        .string()
        .describe(
          "Permission to grant (photos, camera, location, contacts, calendar, microphone, notifications, etc.)"
        ),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, permission, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id, permission };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/permission",
        undefined,
        body
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("set_locale", {
    description: `Set the system locale/language. Android: changes take effect immediately (API ≤ 32) or via setprop (rootable API 33+). iOS physical: changes language and locale via USB (device will briefly restart SpringBoard). iOS simulators: not yet supported.`,
    inputSchema: strictParams({
      lang: z.string().describe("Language code (e.g. 'en', 'ja', 'fr', 'de')"),
      country: z.string().optional().describe("Country code (e.g. 'US', 'JP', 'FR', 'DE')"),
      udid: z.string().optional().describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ lang, country, udid }) => {
    try {
      const body: Record<string, unknown> = { lang };
      if (country) body.country = country;
      if (udid) body.udid = udid;

      const data = await apiRequest("POST", "/api/v1/device/locale", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("set_hardware_keyboard", {
    description: `Attach or detach the simulated hardware keyboard on an iOS simulator — the same switch as the simulator window's "Connect Hardware Keyboard" (shift-cmd-K) toggle — in Device Hub on Xcode 27, which replaced Simulator.app, and in Simulator.app before it. enabled=true hides the software keyboard (smaller UI trees, unobstructed screenshots during form filling); enabled=false restores the software keyboard for focused text fields. NOTE: type_text flips the simulator into hardware-keyboard mode as a side effect of sending key events — call this with enabled=false afterward if a later step expects the software keyboard to be visible.`,
    inputSchema: strictParams({
      enabled: z.boolean().describe("true = attach hardware keyboard (software keyboard hidden), false = detach (software keyboard shows for focused fields)"),
      udid: z.string().optional().describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ enabled, udid }) => {
    try {
      const body: Record<string, unknown> = { enabled };
      if (udid) body.udid = udid;

      const data = await apiRequest("POST", "/api/v1/device/keyboard", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("get_simulator_settings", {
    description: `Read the iOS simulator settings quern can change with set_simulator_setting -- password_autofill, auto_correction, auto_capitalization, smart_punctuation, period_shortcut, predictive_text, spell_check -- and whether each is on or off ("mixed" when the keys it covers disagree, which set_simulator_setting rewrites; null when its file could not be read). Each entry says which runtimes it was verified on and whether that includes this simulator's (verified_here): the storage is undocumented, so an unverified runtime is reported rather than assumed.`,
    inputSchema: strictParams({
      udid: z.string().optional().describe("Target simulator UDID (defaults to active device)"),
    }),
  }, async ({ udid }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/settings", udid ? { udid } : undefined);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("set_simulator_setting", {
    description: `Turn an iOS simulator setting on or off without driving the Settings app. The ones that break automation: password_autofill (iOS's "Save Password?" sheet after a sign-in, invisible in the app's UI tree), auto_correction ("qft" becoming "qty"), auto_capitalization ("qft" becoming "Qft"), smart_punctuation (curly quotes, dashes), period_shortcut (double space becoming ". "), predictive_text, spell_check. Turning the text ones off is how to make type_text's exact read-back hold in fields iOS would otherwise rewrite.

Applying a change needs the simulator rebooted, which ENDS THE RUNNING APP. On a booted simulator the call therefore refuses with an error unless reboot=true -- so call it before launching the app under test, not in the middle of a test. A setting already in place, or a shut-down simulator, needs no reboot. The response says changed and rebooted, and carries a warning when this runtime is not one the setting was verified on. The change lasts until the simulator is erased.`,
    inputSchema: strictParams({
      name: z.enum(["password_autofill", "auto_correction", "auto_capitalization", "smart_punctuation", "period_shortcut", "predictive_text", "spell_check"]).describe("Which setting"),
      value: z.enum(["on", "off"]).describe("The state to set"),
      reboot: z.boolean().default(false).describe("Allow rebooting a booted simulator to apply the change. A reboot ends the running app."),
      udid: z.string().optional().describe("Target simulator UDID (defaults to active device)"),
    }),
  }, async ({ name, value, reboot, udid }) => {
    try {
      const body: Record<string, unknown> = { name, value, reboot };
      if (udid) body.udid = udid;
      const data = await apiRequest("POST", "/api/v1/device/settings", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("set_font_scale", {
    description: `Set the font scale on an Android device or emulator. Takes effect immediately. Standard values: 0.85 (small), 1.0 (default), 1.15 (large), 1.30 (largest). Any float value is accepted.`,
    inputSchema: strictParams({
      scale: z.coerce.number().describe("Font scale factor (1.0 = default)"),
      udid: z.string().optional().describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ scale, udid }) => {
    try {
      const body: Record<string, unknown> = { scale };
      if (udid) body.udid = udid;

      const data = await apiRequest("POST", "/api/v1/device/font-scale", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("set_display_density", {
    description: `Set the display density (DPI) on an Android device or emulator. Takes effect immediately. Common values: 160 (mdpi), 240 (hdpi), 320 (xhdpi), 480 (xxhdpi). Omit dpi to reset to the device's physical default.`,
    inputSchema: strictParams({
      dpi: z.coerce.number().optional().describe("Display density in DPI. Omit to reset to default."),
      udid: z.string().optional().describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ dpi, udid }) => {
    try {
      const body: Record<string, unknown> = {};
      if (dpi !== undefined) body.dpi = dpi;
      if (udid) body.udid = udid;

      const data = await apiRequest("POST", "/api/v1/device/display-density", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("preview_device", {
    description: `Open a live preview window showing a device's screen in real time. iOS physical devices use CoreMediaIO over USB. Booted iOS simulators are supported too, by a different route: quern-media reads the simulator framebuffer and serves it as MJPEG. Android devices (emulators and physical) use scrcpy (requires 'brew install scrcpy'). Multiple devices can be previewed independently. If no UDID is provided, opens preview windows for all connected USB iOS devices -- simulators are not included in that sweep and must be named. An iOS response that concerns one device carries a "kind" field, either 'device' or 'simulator', because a phone and a simulator of the same model report the same name. stop_preview returns it when given a UDID; without one it stops everything and returns an aggregate status with no kind.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe(
          "UDID of a physical device (iOS or Android), a booted iOS simulator, or an Android emulator. If omitted, previews every USB-connected physical iOS device and nothing else -- no simulators, no Android."
        ),
    }),
  }, async ({ udid }) => {
    try {
      const body: Record<string, unknown> = {};
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/preview/start",
        undefined,
        body
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("stop_preview", {
    description: `Stop a live device preview. Accepts the UDID of a physical device or of a booted simulator. If a UDID is provided, stops only that device's preview (others stay running). If no UDID is provided, stops all previews and terminates the preview process.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe(
          "UDID of a specific device to stop previewing. If omitted, stops all previews."
        ),
    }),
  }, async ({ udid }) => {
    try {
      const body: Record<string, unknown> = {};
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/preview/stop",
        undefined,
        body
      );

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("preview_status", {
    description: `Check the status of live device previews. Shows which devices are actively previewing, available devices, and process state.`,
    inputSchema: strictParams({}),
  }, async () => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/preview/status");

      return {
        content: [
          { type: "text" as const, text: JSON.stringify(data, null, 2) },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });
}
