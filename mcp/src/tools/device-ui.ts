import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerDeviceUITools(server: McpServer): void {
  server.registerTool("get_ui_tree", {
    description: `Get the full accessibility tree (all UI elements) from the current screen. Optionally scope to children of a specific element using children_of.

Pass include_raw=true when debugging the platform normalizer itself — e.g., to see whether an Android node carries selected="true" or some other source attribute that didn't make it into our canonical fields. Each element gains an extra_attrs dict of the raw source attributes from the underlying provider (uiautomator2 XML on Android; iOS not yet populated). Default false to keep payloads small.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      children_of: z
        .string()
        .optional()
        .describe("Only return children of the element with this identifier or label"),
      snapshot_depth: z
        .coerce.number()
        .min(1)
        .max(50)
        .optional()
        .describe("WDA accessibility tree depth (1-50, default 10). Lower = faster but may miss labels. Higher = more detail but may hang on complex screens like maps. Only affects physical devices."),
      strategy: z
        .enum(["skeleton"])
        .optional()
        .describe("Use 'skeleton' to skip /source timeout on complex screens (maps with many pins). Returns navigation chrome only. Physical devices only."),
      source_timeout: z
        .coerce.number()
        .min(1)
        .max(60)
        .optional()
        .describe("Override WDA /source timeout in seconds (default: 3s, 6s for older devices). Use 10-15 for slow screens like feeds/lists on older devices. Physical devices only."),
      mode: z
        .enum(["flat"])
        .optional()
        .describe("Use 'flat' to engage the patched idb companion's flat-mode output (idb backend only). Rarely needed: the default path (sim-bridge on Xcode 26+/Apple Silicon, idb elsewhere) already probes hidden tab-bar/nav-bar children. Simulators only."),
      include_raw: z
        .coerce.boolean()
        .optional()
        .describe(
          "Include raw source attributes (extra_attrs) on each element — useful when debugging the normalizer to see what the underlying provider actually emitted. Default false. Currently only Android populates extra_attrs."
        ),
    }),
  }, async ({ udid, children_of, snapshot_depth, strategy, source_timeout, mode, include_raw }) => {
    try {
      const params: Record<string, string> = {};
      if (udid) params.udid = udid;
      if (children_of) params.children_of = children_of;
      if (snapshot_depth !== undefined) params.snapshot_depth = String(snapshot_depth);
      if (strategy) params.strategy = strategy;
      if (source_timeout !== undefined) params.source_timeout = String(source_timeout);
      if (mode) params.mode = mode;
      if (include_raw) params.include_raw = "true";
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
    description: `Get a single element's state without fetching the entire UI tree. More efficient than get_ui_tree when you only need to check one element. Returns the element with its current state (enabled, value, etc.). If multiple elements match, returns the first with a match_count field.`,
    inputSchema: strictParams({
      label: z
        .string()
        .optional()
        .describe("Element label — exact match (case-insensitive). Mutually exclusive with label_contains and label_prefix."),
      label_contains: z
        .string()
        .optional()
        .describe("Substring to search for in element labels (case-insensitive). Mutually exclusive with label and label_prefix."),
      label_prefix: z
        .string()
        .optional()
        .describe("Prefix to match at the start of element labels (case-insensitive). Mutually exclusive with label and label_contains."),
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
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ label, label_contains, label_prefix, identifier, element_type, udid }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/ui/element", {
        label,
        label_contains,
        label_prefix,
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
    description: `Wait for an element to satisfy a condition (server-side polling). Eliminates client-side retry loops and reduces API round-trips. Always returns with matched:true/false - timeouts are not errors. Supports conditions: exists, not_exists, visible, enabled, disabled, value_equals, value_contains.`,
    inputSchema: strictParams({
      label: z
        .string()
        .optional()
        .describe("Element label — exact match (case-insensitive). Mutually exclusive with label_contains and label_prefix."),
      label_contains: z
        .string()
        .optional()
        .describe("Substring to search for in element labels (case-insensitive). Mutually exclusive with label and label_prefix."),
      label_prefix: z
        .string()
        .optional()
        .describe("Prefix to match at the start of element labels (case-insensitive). Mutually exclusive with label and label_contains."),
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
        .coerce.number()
        .default(10)
        .describe("Max wait time in seconds (default 10, max 60)"),
      interval: z
        .coerce.number()
        .default(0.5)
        .describe("Poll interval in seconds (default 0.5)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      mode: z
        .enum(["flat"])
        .optional()
        .describe("Use 'flat' to engage the patched idb companion's flat-mode output (idb backend only). Rarely needed now — the default path probes hidden tab-bar/nav-bar children automatically. Simulators only."),
    }),
  }, async ({
    label,
    label_contains,
    label_prefix,
    identifier,
    element_type,
    condition,
    value,
    timeout,
    interval,
    udid,
    mode,
  }) => {
    try {
      const body: Record<string, unknown> = {
        condition,
        timeout,
        interval,
      };

      if (label !== undefined) body.label = label;
      if (label_contains !== undefined) body.label_contains = label_contains;
      if (label_prefix !== undefined) body.label_prefix = label_prefix;
      if (identifier !== undefined) body.identifier = identifier;
      if (element_type !== undefined) body.type = element_type;
      if (value !== undefined) body.value = value;
      if (mode !== undefined) body.mode = mode;
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
    description: `Get an LLM-optimized text description of the current screen, including interactive elements and their locations. Uses smart truncation with prioritization (buttons with identifiers > form inputs > generic buttons > static text). Navigation chrome (tab bars, nav bars) is always included regardless of limit.

This is the recommended first step before interacting with UI. Use this to discover element labels and identifiers, then use tap_element to tap by name instead of coordinates.`,
    inputSchema: strictParams({
      max_elements: z
        .coerce.number()
        .default(20)
        .describe("Maximum interactive elements to include (0 = unlimited, default 20)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      snapshot_depth: z
        .coerce.number()
        .min(1)
        .max(50)
        .optional()
        .describe("WDA accessibility tree depth (1-50, default 10). Lower = faster but may miss labels. Higher = more detail but may hang on complex screens like maps. Only affects physical devices."),
      strategy: z
        .enum(["skeleton"])
        .optional()
        .describe("Use 'skeleton' to skip /source timeout on complex screens (maps with many pins). Returns navigation chrome only. Physical devices only."),
      source_timeout: z
        .coerce.number()
        .min(1)
        .max(60)
        .optional()
        .describe("Override WDA /source timeout in seconds (default: 3s, 6s for older devices). Use 10-15 for slow screens like feeds/lists on older devices. Physical devices only."),
      mode: z
        .enum(["flat"])
        .optional()
        .describe("Use 'flat' to engage the patched idb companion's flat-mode output (idb backend only). Rarely needed: the default path (sim-bridge on Xcode 26+/Apple Silicon, idb elsewhere) already probes hidden tab-bar/nav-bar children. Simulators only."),
      identify: z
        .boolean()
        .optional()
        .describe("Match screen against loaded landmarks. Adds identified_as and confidence fields to the response."),
    }),
  }, async ({ max_elements, udid, snapshot_depth, strategy, source_timeout, mode, identify }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/screen-summary", {
        max_elements,
        udid,
        snapshot_depth,
        strategy,
        source_timeout,
        mode,
        identify,
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
    description: `Tap at specific screen coordinates on the simulator.

PREFER tap_element over this tool. Use get_screen_summary to find element labels/identifiers, then tap_element to tap by name. Only use coordinate tap as a last resort when tap_element cannot find the element.

If coordinate taps are not landing on the expected element, use take_annotated_screenshot to see exact element bounding boxes overlaid on the screen. Read the element's position as a fraction of the screen (e.g. iPhone 12: 390×844 pt, iPhone 15 Pro: 393×852 pt) to calculate correct tap coordinates, then retry.`,
    inputSchema: strictParams({
      x: z.coerce.number().describe("X coordinate"),
      y: z.coerce.number().describe("Y coordinate"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
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
    description: `Find a UI element by label or accessibility identifier and tap its center. Returns "ambiguous" with match list if multiple elements match — use element_type (e.g., "Button", "TextField", "StaticText") to narrow results.

This is the PREFERRED way to tap UI elements. Use get_screen_summary first to discover element labels/identifiers, then use this tool. Avoid using coordinate-based tap unless this tool cannot find the element.

Label matching modes (mutually exclusive — use only one):
- label: exact match (case-insensitive)
- label_contains: substring match (case-insensitive) — useful for elements with long, dynamic labels
- label_prefix: prefix match (case-insensitive) — useful when the label starts with a stable string but has variable content after`,
    inputSchema: strictParams({
      label: z
        .string()
        .optional()
        .describe("Element label text — exact match (case-insensitive). Mutually exclusive with label_contains and label_prefix."),
      label_contains: z
        .string()
        .optional()
        .describe("Substring to search for in element labels (case-insensitive). Mutually exclusive with label and label_prefix."),
      label_prefix: z
        .string()
        .optional()
        .describe("Prefix to match at the start of element labels (case-insensitive). Mutually exclusive with label and label_contains."),
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
        .describe("Target device UDID (defaults to active device)"),
      source_timeout: z
        .coerce.number()
        .min(1)
        .max(60)
        .optional()
        .describe("Override WDA /source timeout in seconds. Use 10-15 for slow screens on older devices. Physical devices only."),
      value: z
        .string()
        .optional()
        .describe('For switches/toggles: desired value ("1" = on, "0" = off). Checks current state first and skips the tap if already set. Returns status "already_set" if no tap was needed.'),
      include_screen_context: z
        .boolean()
        .default(false)
        .describe("Include a screen summary in the response after the tap completes. Useful for verifying navigation."),
      capture_screenshots: z
        .boolean()
        .default(false)
        .describe("Capture before/after screenshots around the tap. Returns file paths in screenshots.before and screenshots.after."),
      settle_delay: z
        .coerce.number()
        .min(0)
        .max(10)
        .optional()
        .describe("Seconds to wait before capturing after screenshot/screen context (default 1.0). Increase for slow devices or complex transitions."),
    }),
  }, async ({ label, label_contains, label_prefix, identifier, element_type, udid, source_timeout, value, include_screen_context, capture_screenshots, settle_delay }) => {
    try {
      const body: Record<string, unknown> = {};
      if (label) body.label = label;
      if (label_contains) body.label_contains = label_contains;
      if (label_prefix) body.label_prefix = label_prefix;
      if (identifier) body.identifier = identifier;
      if (element_type) body.element_type = element_type;
      if (udid) body.udid = udid;
      if (source_timeout) body.source_timeout = source_timeout;
      if (value !== undefined) body.value = value;
      if (include_screen_context) body.include_screen_context = true;
      if (capture_screenshots) body.capture_screenshots = true;
      if (settle_delay !== undefined) body.settle_delay = settle_delay;

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
    description: `Perform a swipe gesture from one point to another.`,
    inputSchema: strictParams({
      start_x: z.coerce.number().describe("Starting X coordinate"),
      start_y: z.coerce.number().describe("Starting Y coordinate"),
      end_x: z.coerce.number().describe("Ending X coordinate"),
      end_y: z.coerce.number().describe("Ending Y coordinate"),
      duration: z
        .coerce.number()
        .default(0.5)
        .describe("Swipe duration in seconds (default 0.5)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
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
    description: `Type text into the currently focused input field.`,
    inputSchema: strictParams({
      text: z.string().describe("Text to type"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
      include_screen_context: z
        .boolean()
        .default(false)
        .describe("Include a screen summary in the response after typing. Useful for detecting autocorrect issues."),
      capture_screenshots: z
        .boolean()
        .default(false)
        .describe("Capture before/after screenshots around the text entry."),
      settle_delay: z
        .coerce.number()
        .min(0)
        .max(10)
        .optional()
        .describe("Seconds to wait before capturing after screenshot/screen context (default 1.0)."),
    }),
  }, async ({ text, udid, include_screen_context, capture_screenshots, settle_delay }) => {
    try {
      const body: Record<string, unknown> = { text };
      if (udid) body.udid = udid;
      if (include_screen_context) body.include_screen_context = true;
      if (capture_screenshots) body.capture_screenshots = true;
      if (settle_delay !== undefined) body.settle_delay = settle_delay;

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
    description: `Clear all text in the currently focused input field (select-all + delete). Use this before type_text when a field has pre-existing content you want to replace. Note: Secure text fields (passwords) may not support select-all.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
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
    description: `Press a hardware button on the device. iOS: HOME, LOCK, SIDE_BUTTON, SIRI, APPLE_PAY. Android: home, back, recents, volumeUp, volumeDown, power, enter, delete, menu.`,
    inputSchema: strictParams({
      button: z
        .string()
        .describe(
          "Button name. iOS: HOME, LOCK, SIDE_BUTTON, SIRI, APPLE_PAY. Android: home, back, recents, volumeUp, volumeDown, power, enter, delete, menu"
        ),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
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
