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
    "get_ui_tree",
    `Get the full accessibility tree (all UI elements) from the current screen. Optionally scope to children of a specific element using children_of. Requires idb.`,
    {
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
      children_of: z
        .string()
        .optional()
        .describe("Only return children of the element with this identifier or label"),
    },
    async ({ udid, children_of }) => {
      try {
        const params: Record<string, string> = {};
        if (udid) params.udid = udid;
        if (children_of) params.children_of = children_of;
        const data = await apiRequest("GET", "/api/v1/device/ui", params);

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
    "get_element_state",
    `Get a single element's state without fetching the entire UI tree. More efficient than get_ui_tree when you only need to check one element. Returns the element with its current state (enabled, value, etc.). If multiple elements match, returns the first with a match_count field. Requires idb.`,
    {
      label: z
        .string()
        .optional()
        .describe("Element label (case-insensitive)"),
      identifier: z
        .string()
        .optional()
        .describe("Element identifier (case-sensitive)"),
      element_type: z
        .string()
        .optional()
        .describe("Element type to narrow results (e.g., 'Button', 'TextField')"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ label, identifier, element_type, udid }) => {
      try {
        const data = await apiRequest("GET", "/api/v1/device/ui/element", {
          label,
          identifier,
          type: element_type,
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
    "wait_for_element",
    `Wait for an element to satisfy a condition (server-side polling). Eliminates client-side retry loops and reduces API round-trips. Always returns with matched:true/false - timeouts are not errors. Supports conditions: exists, not_exists, visible, enabled, disabled, value_equals, value_contains. Requires idb.`,
    {
      label: z
        .string()
        .optional()
        .describe("Element label (case-insensitive)"),
      identifier: z
        .string()
        .optional()
        .describe("Element identifier (case-sensitive)"),
      element_type: z
        .string()
        .optional()
        .describe("Element type to narrow results (e.g., 'Button', 'TextField')"),
      condition: z
        .enum([
          "exists",
          "not_exists",
          "visible",
          "enabled",
          "disabled",
          "value_equals",
          "value_contains",
        ])
        .describe("Condition to wait for"),
      value: z
        .string()
        .optional()
        .describe("Required for value_equals and value_contains conditions"),
      timeout: z
        .number()
        .default(10)
        .describe("Max wait time in seconds (default 10, max 60)"),
      interval: z
        .number()
        .default(0.5)
        .describe("Poll interval in seconds (default 0.5)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({
      label,
      identifier,
      element_type,
      condition,
      value,
      timeout,
      interval,
      udid,
    }) => {
      try {
        const body: Record<string, unknown> = {
          condition,
          timeout,
          interval,
        };

        if (label !== undefined) body.label = label;
        if (identifier !== undefined) body.identifier = identifier;
        if (element_type !== undefined) body.type = element_type;
        if (value !== undefined) body.value = value;
        if (udid !== undefined) body.udid = udid;

        const data = await apiRequest(
          "POST",
          "/api/v1/device/ui/wait-for-element",
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
    "get_screen_summary",
    `Get an LLM-optimized text description of the current screen, including interactive elements and their locations. Uses smart truncation with prioritization (buttons with identifiers > form inputs > generic buttons > static text). Navigation chrome (tab bars, nav bars) is always included regardless of limit. Requires idb.

This is the recommended first step before interacting with UI. Use this to discover element labels and identifiers, then use tap_element to tap by name instead of coordinates.`,
    {
      max_elements: z
        .number()
        .default(20)
        .describe("Maximum interactive elements to include (0 = unlimited, default 20)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ max_elements, udid }) => {
      try {
        const data = await apiRequest("GET", "/api/v1/device/screen-summary", {
          max_elements,
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
    "tap",
    `Tap at specific screen coordinates on the simulator. Requires idb.

PREFER tap_element over this tool. Use get_screen_summary to find element labels/identifiers, then tap_element to tap by name. Only use coordinate tap as a last resort when tap_element cannot find the element.`,
    {
      x: z.number().describe("X coordinate"),
      y: z.number().describe("Y coordinate"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ x, y, udid }) => {
      try {
        const body: Record<string, unknown> = { x, y };
        if (udid) body.udid = udid;

        const data = await apiRequest(
          "POST",
          "/api/v1/device/ui/tap",
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
    "tap_element",
    `Find a UI element by label or accessibility identifier and tap its center. Returns "ambiguous" with match list if multiple elements match — use element_type (e.g., "Button", "TextField", "StaticText") to narrow results. Requires idb.

This is the PREFERRED way to tap UI elements. Use get_screen_summary first to discover element labels/identifiers, then use this tool. Avoid using coordinate-based tap unless this tool cannot find the element.`,
    {
      label: z
        .string()
        .optional()
        .describe("Element label text to search for"),
      identifier: z
        .string()
        .optional()
        .describe("Accessibility identifier to search for"),
      element_type: z
        .string()
        .optional()
        .describe('Element type to filter by (e.g. "Button", "TextField")'),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ label, identifier, element_type, udid }) => {
      try {
        const body: Record<string, unknown> = {};
        if (label) body.label = label;
        if (identifier) body.identifier = identifier;
        if (element_type) body.element_type = element_type;
        if (udid) body.udid = udid;

        const data = await apiRequest(
          "POST",
          "/api/v1/device/ui/tap-element",
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
    "swipe",
    `Perform a swipe gesture from one point to another. Requires idb.`,
    {
      start_x: z.number().describe("Starting X coordinate"),
      start_y: z.number().describe("Starting Y coordinate"),
      end_x: z.number().describe("Ending X coordinate"),
      end_y: z.number().describe("Ending Y coordinate"),
      duration: z
        .number()
        .default(0.5)
        .describe("Swipe duration in seconds (default 0.5)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ start_x, start_y, end_x, end_y, duration, udid }) => {
      try {
        const body: Record<string, unknown> = {
          start_x,
          start_y,
          end_x,
          end_y,
          duration,
        };
        if (udid) body.udid = udid;

        const data = await apiRequest(
          "POST",
          "/api/v1/device/ui/swipe",
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
    "type_text",
    `Type text into the currently focused input field. Requires idb.`,
    {
      text: z.string().describe("Text to type"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ text, udid }) => {
      try {
        const body: Record<string, unknown> = { text };
        if (udid) body.udid = udid;

        const data = await apiRequest(
          "POST",
          "/api/v1/device/ui/type",
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
    "clear_text",
    `Clear all text in the currently focused input field (select-all + delete). Use this before type_text when a field has pre-existing content you want to replace. Note: Secure text fields (passwords) may not support select-all. Requires idb.`,
    {
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ udid }) => {
      try {
        const body: Record<string, unknown> = {};
        if (udid) body.udid = udid;

        const data = await apiRequest(
          "POST",
          "/api/v1/device/ui/clear",
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
    "press_button",
    `Press a hardware button on the simulator (e.g. HOME, LOCK, SIRI, APPLE_PAY). Requires idb.`,
    {
      button: z
        .string()
        .describe(
          "Button name (HOME, LOCK, SIDE_BUTTON, SIRI, APPLE_PAY)"
        ),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    },
    async ({ button, udid }) => {
      try {
        const body: Record<string, unknown> = { button };
        if (udid) body.udid = udid;

        const data = await apiRequest(
          "POST",
          "/api/v1/device/ui/press",
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
