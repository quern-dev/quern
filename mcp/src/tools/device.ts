import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { discoverServer } from "../config.js";
import { apiRequest } from "../http.js";

export function registerDeviceTools(server: McpServer): void {
  server.tool(
    "list_devices",
    `List available iOS simulators and tool availability (simctl, idb). Returns device UDIDs, names, states, and OS versions.`,
    {
      state: z
        .enum(["booted", "shutdown"])
        .optional()
        .describe("Filter by device state"),
      type: z
        .enum(["simulator", "device"])
        .optional()
        .describe("Filter by device type"),
    },
    async ({ state, type }) => {
      try {
        const data = (await apiRequest("GET", "/api/v1/device/list")) as {
          devices: Array<Record<string, unknown>>;
          tools: Record<string, boolean>;
          active_udid: string | null;
        };

        let devices = data.devices;
        if (state) {
          devices = devices.filter((d) => d.state === state);
        }
        if (type) {
          devices = devices.filter((d) => d.device_type === type);
        }

        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify(
                { devices, tools: data.tools, active_udid: data.active_udid },
                null,
                2
              ),
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
    }
  );

  server.tool(
    "boot_device",
    `Boot an iOS simulator by UDID or name.`,
    {
      udid: z.string().optional().describe("Device UDID to boot"),
      name: z
        .string()
        .optional()
        .describe('Device name to boot (e.g. "iPhone 16 Pro")'),
    },
    async ({ udid, name }) => {
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
    }
  );

  server.tool(
    "shutdown_device",
    `Shutdown an iOS simulator.`,
    {
      udid: z.string().describe("Device UDID to shutdown"),
    },
    async ({ udid }) => {
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
    }
  );

  server.tool(
    "install_app",
    `Install an app (.app or .ipa) on a simulator.`,
    {
      app_path: z.string().describe("Path to the .app or .ipa file"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ app_path, udid }) => {
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
    }
  );

  server.tool(
    "launch_app",
    `Launch an app by bundle ID on a simulator.

NOTE: If you want to capture network traffic from this app:
1. Ensure the proxy is running (start_proxy)
2. Enable system proxy (configure_system_proxy)
3. Launch the app (this tool)
4. When done, disable system proxy (unconfigure_system_proxy)`,
    {
      bundle_id: z.string().describe("App bundle identifier (e.g. com.example.MyApp)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ bundle_id, udid }) => {
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
    }
  );

  server.tool(
    "terminate_app",
    `Terminate a running app by bundle ID.`,
    {
      bundle_id: z.string().describe("App bundle identifier"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ bundle_id, udid }) => {
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
    }
  );

  server.tool(
    "list_apps",
    `List installed apps on a simulator.`,
    {
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ udid }) => {
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
    }
  );

  server.tool(
    "take_screenshot",
    `Capture a screenshot from the simulator. Returns the image as base64-encoded data.`,
    {
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
      format: z
        .enum(["png", "jpeg"])
        .default("png")
        .describe("Image format"),
      scale: z
        .number()
        .min(0.1)
        .max(1.0)
        .default(0.5)
        .describe("Scale factor (0.1-1.0, default 0.5)"),
      quality: z
        .number()
        .min(1)
        .max(100)
        .default(85)
        .describe("JPEG quality (1-100, ignored for PNG)"),
    },
    async ({ udid, format, scale, quality }) => {
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
    }
  );

  server.tool(
    "set_location",
    `Set the simulated GPS location on a simulator.`,
    {
      latitude: z.number().describe("GPS latitude"),
      longitude: z.number().describe("GPS longitude"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ latitude, longitude, udid }) => {
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
    }
  );

  server.tool(
    "grant_permission",
    `Grant an app permission on a simulator (e.g. photos, camera, location, contacts, calendar, microphone, notifications).`,
    {
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
    },
    async ({ bundle_id, permission, udid }) => {
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
    }
  );
}
