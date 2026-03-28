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
    description: `Erase a simulator, resetting it to factory state. All apps, data, and settings are removed. The simulator is shut down automatically before erasing. Simulator only — not supported for physical devices or Android.`,
    inputSchema: strictParams({
      udid: z.string().describe("Simulator UDID to erase"),
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
        .describe("Environment variables to pass to the app process (iOS simulators only). Uses the SIMCTL_CHILD_ prefix convention. QUERN_AUTOMATION=YES is always set automatically."),
    }),
  }, async ({ bundle_id, udid, env }) => {
    try {
      const body: Record<string, unknown> = { bundle_id };
      if (udid) body.udid = udid;
      if (env) body.env = env;

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
    description: `Capture a screenshot with accessibility annotations overlaid. Draws red bounding boxes and labels (element type + accessibility label) on interactive UI elements (buttons, text fields, switches, etc.). Useful for debugging UI automation issues — visually confirms what the accessibility tree sees vs. what's on screen. Always returns PNG.`,
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
      save_path: z
        .string()
        .optional()
        .describe(
          "Save screenshot to this file path instead of returning base64. Parent directories are created automatically."
        ),
    }),
  }, async ({ udid, scale, quality, save_path }) => {
    try {
      const srv = discoverServer();
      const url = new URL("/api/v1/device/screenshot/annotated", srv.url);
      if (udid) url.searchParams.set("udid", udid);
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
    description: `Open a URL on a simulator or emulator using the platform's default handler. Supports any URI scheme the device has a handler for: https://, geo: (maps), tel:, mailto:, custom app URL schemes, deep links, universal links, and settings URIs (Android: android.settings.* actions, iOS: App-prefs:).

Note: tel: and mailto: are unavailable on iOS simulators (no Phone or Mail app).

Examples:
- Web: "https://example.com"
- Maps: "geo:48.8584,2.2945?z=15" (Android) or "maps://?ll=48.8584,2.2945&z=15" (iOS)
- Settings: "App-prefs:WIFI" (iOS) — Android uses action-based intents via adb
- Deep link: "myapp://path/to/screen"`,
    inputSchema: strictParams({
      url: z.string().describe(
        "URL or URI to open (e.g. https://example.com, geo:48.8,2.3?z=15, maps://?ll=48.8,2.3)"
      ),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ url, udid }) => {
    try {
      const body: Record<string, unknown> = { url };
      if (udid) body.udid = udid;

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
    description: `Open a live preview window showing a device's screen in real time. iOS physical devices use CoreMediaIO (USB only, not simulators). Android devices (emulators and physical) use scrcpy (requires 'brew install scrcpy'). Multiple devices can be previewed independently. If no UDID is provided, opens preview windows for all connected USB iOS devices.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe(
          "UDID of a physical device to preview. If omitted, previews all USB-connected devices."
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
    description: `Stop a live device preview. If a UDID is provided, stops only that device's preview (others stay running). If no UDID is provided, stops all previews and terminates the preview process.`,
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
