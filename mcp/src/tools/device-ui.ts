import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerDeviceUITools(server: McpServer): void {
  server.registerTool("get_ui_tree", {
    description: `Get the full accessibility tree (all UI elements) from the current screen. Optionally scope to children of a specific element using children_of. Requires idb.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
      children_of: z
        .string()
        .optional()
        .describe("Only return children of the element with this identifier or label"),
      snapshot_depth: z
        .number()
        .min(1)
        .max(50)
        .optional()
        .describe("WDA accessibility tree depth (1-50, default 10). Lower = faster but may miss labels. Higher = more detail but may hang on complex screens like maps. Only affects physical devices."),
      strategy: z
        .enum(["skeleton"])
        .optional()
        .describe("Use 'skeleton' to skip /source timeout on complex screens (maps with many pins). Returns navigation chrome only. Physical devices only."),
    }),
  }, async ({ udid, children_of, snapshot_depth, strategy }) => {
    try {
      const params: Record<string, string> = {};
      if (udid) params.udid = udid;
      if (children_of) params.children_of = children_of;
      if (snapshot_depth !== undefined) params.snapshot_depth = String(snapshot_depth);
      if (strategy) params.strategy = strategy;
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
  });

  server.registerTool("get_element_state", {
    description: `Get a single element's state without fetching the entire UI tree. More efficient than get_ui_tree when you only need to check one element. Returns the element with its current state (enabled, value, etc.). If multiple elements match, returns the first with a match_count field. Requires idb.`,
    inputSchema: strictParams({
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
    }),
  }, async ({ label, identifier, element_type, udid }) => {
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
  });

  server.registerTool("wait_for_element", {
    description: `Wait for an element to satisfy a condition (server-side polling). Eliminates client-side retry loops and reduces API round-trips. Always returns with matched:true/false - timeouts are not errors. Supports conditions: exists, not_exists, visible, enabled, disabled, value_equals, value_contains. Requires idb.`,
    inputSchema: strictParams({
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
    }),
  }, async ({
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
  });

  server.registerTool("get_screen_summary", {
    description: `Get an LLM-optimized text description of the current screen, including interactive elements and their locations. Uses smart truncation with prioritization (buttons with identifiers > form inputs > generic buttons > static text). Navigation chrome (tab bars, nav bars) is always included regardless of limit. Requires idb.

This is the recommended first step before interacting with UI. Use this to discover element labels and identifiers, then use tap_element to tap by name instead of coordinates.`,
    inputSchema: strictParams({
      max_elements: z
        .number()
        .default(20)
        .describe("Maximum interactive elements to include (0 = unlimited, default 20)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
      snapshot_depth: z
        .number()
        .min(1)
        .max(50)
        .optional()
        .describe("WDA accessibility tree depth (1-50, default 10). Lower = faster but may miss labels. Higher = more detail but may hang on complex screens like maps. Only affects physical devices."),
      strategy: z
        .enum(["skeleton"])
        .optional()
        .describe("Use 'skeleton' to skip /source timeout on complex screens (maps with many pins). Returns navigation chrome only. Physical devices only."),
    }),
  }, async ({ max_elements, udid, snapshot_depth, strategy }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/screen-summary", {
        max_elements,
        udid,
        snapshot_depth,
        strategy,
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

  server.registerTool("tap", {
    description: `Tap at specific screen coordinates on the simulator. Requires idb.

PREFER tap_element over this tool. Use get_screen_summary to find element labels/identifiers, then tap_element to tap by name. Only use coordinate tap as a last resort when tap_element cannot find the element.

If coordinate taps are not landing on the expected element, use take_annotated_screenshot to see exact element bounding boxes overlaid on the screen. Read the element's position as a fraction of the screen (e.g. iPhone 12: 390×844 pt, iPhone 15 Pro: 393×852 pt) to calculate correct tap coordinates, then retry.`,
    inputSchema: strictParams({
      x: z.number().describe("X coordinate"),
      y: z.number().describe("Y coordinate"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    }),
  }, async ({ x, y, udid }) => {
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
  });

  server.registerTool("tap_element", {
    description: `Find a UI element by label or accessibility identifier and tap its center. Returns "ambiguous" with match list if multiple elements match — use element_type (e.g., "Button", "TextField", "StaticText") to narrow results. Requires idb.

This is the PREFERRED way to tap UI elements. Use get_screen_summary first to discover element labels/identifiers, then use this tool. Avoid using coordinate-based tap unless this tool cannot find the element.`,
    inputSchema: strictParams({
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
    }),
  }, async ({ label, identifier, element_type, udid }) => {
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
  });

  server.registerTool("swipe", {
    description: `Perform a swipe gesture from one point to another. Requires idb.`,
    inputSchema: strictParams({
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
    }),
  }, async ({ start_x, start_y, end_x, end_y, duration, udid }) => {
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
  });

  server.registerTool("type_text", {
    description: `Type text into the currently focused input field. Requires idb.`,
    inputSchema: strictParams({
      text: z.string().describe("Text to type"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    }),
  }, async ({ text, udid }) => {
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
  });

  server.registerTool("clear_text", {
    description: `Clear all text in the currently focused input field (select-all + delete). Use this before type_text when a field has pre-existing content you want to replace. Note: Secure text fields (passwords) may not support select-all. Requires idb.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    }),
  }, async ({ udid }) => {
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
  });

  server.registerTool("press_button", {
    description: `Press a hardware button on the simulator (e.g. HOME, LOCK, SIRI, APPLE_PAY). Requires idb.`,
    inputSchema: strictParams({
      button: z
        .string()
        .describe(
          "Button name (HOME, LOCK, SIDE_BUTTON, SIRI, APPLE_PAY)"
        ),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (auto-resolves if omitted)"),
    }),
  }, async ({ button, udid }) => {
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
  });
}
