import { writeFile, mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { discoverServer } from "../config.js";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerDeviceTools(server: McpServer): void {
  server.registerTool("list_devices", {
    description: `List available iOS simulators and physical devices, plus tool availability (simctl, idb, devicectl). Returns device UDIDs, names, states, and OS versions.`,
    inputSchema: strictParams({
      state: z
        .enum(["booted", "shutdown"])
        .optional()
        .describe("Filter by device state"),
      type: z
        .enum(["simulator", "device"])
        .optional()
        .describe("Filter by device type"),
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
  }, async ({ state, type, cert_installed, include_disconnected }) => {
    try {
      const params: Record<string, string | number | boolean | undefined> = {};
      if (state) params.state = state;
      if (type) params.device_type = type;
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
    description: `Boot an iOS simulator by UDID or name. Simulator only — not supported for physical devices.`,
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
    description: `Shutdown an iOS simulator. Simulator only — not supported for physical devices.`,
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

  server.registerTool("install_app", {
    description: `Install an app (.app or .ipa) on a simulator or physical device.`,
    inputSchema: strictParams({
      app_path: z.string().describe("Path to the .app or .ipa file"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
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
    description: `Launch an app by bundle ID on a simulator or physical device.

NOTE: If you want to capture network traffic from this app:
1. Ensure the proxy is running (start_proxy)
2. Enable system proxy (configure_system_proxy)
3. Launch the app (this tool)
4. When done, disable system proxy (unconfigure_system_proxy)`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier (e.g. com.example.MyApp)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    }),
  }, async ({ bundle_id, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id };
      if (udid) body.udid = udid;

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
    description: `Terminate a running app by bundle ID.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
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
    description: `Uninstall an app from a simulator or physical device by bundle ID.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
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
    description: `List installed apps on a simulator or physical device.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
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
        .describe("Target device UDID (auto-resolves if omitted)"),
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
        .describe("Target device UDID (auto-resolves if omitted)"),
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
    description: `Set the simulated GPS location on a simulator.`,
    inputSchema: strictParams({
      latitude: z.coerce.number().describe("GPS latitude"),
      longitude: z.coerce.number().describe("GPS longitude"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
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

  server.registerTool("grant_permission", {
    description: `Grant an app permission on a simulator (e.g. photos, camera, location, contacts, calendar, microphone, notifications).`,
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
        .describe("Target device UDID (auto-resolves if omitted)"),
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

  server.registerTool("preview_device", {
    description: `Open a live macOS preview window showing a physical iOS device's screen in real time over USB. Uses CoreMediaIO screen capture — only works for physical devices connected via USB (not simulators). Compiles the preview binary on first use (~5s). Device discovery takes ~3s on first launch (cached thereafter). Multiple devices can be previewed independently. If no UDID is provided, opens preview windows for all connected USB devices.`,
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
