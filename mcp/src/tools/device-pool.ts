import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerDevicePoolTools(server: McpServer): void {
  server.registerTool("resolve_device", {
    description: `Smartly find a device matching criteria. This is the preferred way to get a
device — it handles booting automatically. The resolved device becomes the
active device, so subsequent tools (take_screenshot, tap, etc.) use it by
default without needing to pass udid.`,
    inputSchema: strictParams({
      name: z
        .string()
        .optional()
        .describe("Device name pattern (e.g., 'iPhone 16 Pro'). Exact matches are preferred over substring matches."),
      os_version: z
        .string()
        .optional()
        .describe("OS version prefix — '18' matches 18.x, '18.2' matches 18.2 exactly. Accepts both '18.2' and 'iOS 18.2'."),
      device_family: z
        .string()
        .optional()
        .describe("Device family filter: 'iPhone', 'iPad', 'Apple Watch', 'Apple TV'. Defaults to 'iPhone' (configurable in ~/.quern/config.json)."),
      type: z
        .enum(["simulator", "device"])
        .optional()
        .default("simulator")
        .describe("Device type filter. Defaults to 'simulator' to avoid accidentally targeting physical devices."),
      auto_boot: z
        .coerce.boolean()
        .optional()
        .default(true)
        .describe(
          "Boot a matching shutdown device if no booted ones available (default: true)"
        ),
    }),
  }, async ({ name, os_version, device_family, type, auto_boot }) => {
      try {
        const body: Record<string, unknown> = {};
        if (name) body.name = name;
        if (os_version) body.os_version = os_version;
        if (device_family) body.device_family = device_family;
        if (type) body.device_type = type;
        if (auto_boot !== undefined) body.auto_boot = auto_boot;

        const data = await apiRequest(
          "POST",
          "/api/v1/devices/resolve",
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

  server.registerTool("ensure_devices", {
    description: `Ensure N devices matching criteria are booted and ready. Use this to set up
parallel test execution — it finds available devices and boots more if needed.
The first device becomes the active device for subsequent tool calls.`,
    inputSchema: strictParams({
      count: z
        .coerce.number()
        .min(1)
        .max(10)
        .describe("Number of devices needed"),
      name: z
        .string()
        .optional()
        .describe("Device name pattern (e.g., 'iPhone 16 Pro'). Exact matches are preferred over substring matches."),
      os_version: z
        .string()
        .optional()
        .describe("OS version prefix — '18' matches 18.x, '18.2' matches 18.2 exactly. Accepts both '18.2' and 'iOS 18.2'."),
      device_family: z
        .string()
        .optional()
        .describe("Device family filter: 'iPhone', 'iPad', 'Apple Watch', 'Apple TV'. Defaults to 'iPhone' (configurable in ~/.quern/config.json)."),
      type: z
        .enum(["simulator", "device"])
        .optional()
        .default("simulator")
        .describe("Device type filter. Defaults to 'simulator' to avoid accidentally targeting physical devices."),
      auto_boot: z
        .coerce.boolean()
        .optional()
        .default(true)
        .describe("Boot shutdown devices if not enough booted ones"),
    }),
  }, async ({ count, name, os_version, device_family, type, auto_boot }) => {
      try {
        const body: Record<string, unknown> = { count };
        if (name) body.name = name;
        if (os_version) body.os_version = os_version;
        if (device_family) body.device_family = device_family;
        if (type) body.device_type = type;
        if (auto_boot !== undefined) body.auto_boot = auto_boot;

        const data = await apiRequest(
          "POST",
          "/api/v1/devices/ensure",
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
