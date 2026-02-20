import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";

export function registerDevicePoolTools(server: McpServer): void {
  server.tool(
    "list_device_pool",
    `List all devices in the pool with their claim status. Shows which devices are available for claiming and which are already claimed by sessions. Use this to see what devices exist before claiming one.`,
    {
      state: z
        .enum(["booted", "shutdown"])
        .optional()
        .describe("Filter by boot state"),
      claimed: z
        .enum(["claimed", "available"])
        .optional()
        .describe("Filter by claim status"),
      type: z
        .enum(["simulator", "device"])
        .optional()
        .describe("Filter by device type. Default: no filter (shows all)."),
    },
    async ({ state, claimed, type }) => {
      try {
        const data = await apiRequest("GET", "/api/v1/devices/pool", {
          state,
          claimed,
          device_type: type,
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
    "claim_device",
    `Claim a device for exclusive use by a session. Once claimed, no other session can use this device until it's released. Essential for parallel test execution to ensure device isolation. Provide either a specific UDID or a name pattern.`,
    {
      session_id: z.string().describe("Session ID claiming the device"),
      udid: z
        .string()
        .optional()
        .describe("Specific device UDID to claim"),
      name: z
        .string()
        .optional()
        .describe("Device name pattern to match (e.g., 'iPhone 16 Pro')"),
      device_family: z
        .string()
        .optional()
        .describe("Device family filter: 'iPhone', 'iPad', 'Apple Watch', 'Apple TV'. Defaults to 'iPhone' (configurable in ~/.quern/config.json)."),
      type: z
        .enum(["simulator", "device"])
        .optional()
        .describe("Filter by device type. Omit to allow either type."),
    },
    async ({ session_id, udid, name, device_family, type }) => {
      try {
        const body: Record<string, unknown> = { session_id };
        if (udid) body.udid = udid;
        if (name) body.name = name;
        if (device_family) body.device_family = device_family;
        if (type) body.device_type = type;

        const data = await apiRequest(
          "POST",
          "/api/v1/devices/claim",
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
    "release_device",
    `Release a claimed device back to the pool, making it available for other sessions. Always release devices when done to avoid resource exhaustion. Devices are also auto-released after 30 minutes of inactivity.`,
    {
      udid: z.string().describe("Device UDID to release"),
      session_id: z
        .string()
        .optional()
        .describe("Session ID releasing the device (for validation)"),
    },
    async ({ udid, session_id }) => {
      try {
        const body: Record<string, unknown> = { udid };
        if (session_id) body.session_id = session_id;

        const data = await apiRequest(
          "POST",
          "/api/v1/devices/release",
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
    "resolve_device",
    `Smartly find and optionally claim a device matching criteria. This is the
preferred way to get a device — it handles booting, waiting, and claiming
automatically. Use this instead of manually listing the pool and claiming.`,
    {
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
        .boolean()
        .optional()
        .default(false)
        .describe(
          "Boot a matching shutdown device if no booted ones available"
        ),
      wait_if_busy: z
        .boolean()
        .optional()
        .default(false)
        .describe("Wait for a claimed device to be released"),
      wait_timeout: z
        .number()
        .optional()
        .default(30)
        .describe("Max seconds to wait if wait_if_busy is true"),
      session_id: z
        .string()
        .optional()
        .describe("Claim the device for this session. Devices already claimed by this session are reused without re-claiming."),
    },
    async ({ name, os_version, device_family, type, auto_boot, wait_if_busy, wait_timeout, session_id }) => {
      try {
        const body: Record<string, unknown> = {};
        if (name) body.name = name;
        if (os_version) body.os_version = os_version;
        if (device_family) body.device_family = device_family;
        if (type) body.device_type = type;
        if (auto_boot !== undefined) body.auto_boot = auto_boot;
        if (wait_if_busy !== undefined) body.wait_if_busy = wait_if_busy;
        if (wait_timeout !== undefined) body.wait_timeout = wait_timeout;
        if (session_id) body.session_id = session_id;

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

  server.tool(
    "ensure_devices",
    `Ensure N devices matching criteria are booted and ready. Use this to set up
parallel test execution — it finds available devices, boots more if needed,
and optionally claims them all for a session.`,
    {
      count: z
        .number()
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
        .boolean()
        .optional()
        .default(true)
        .describe("Boot shutdown devices if not enough booted ones"),
      session_id: z
        .string()
        .optional()
        .describe("Claim all devices for this session. Devices already claimed by this session are reused without re-claiming."),
    },
    async ({ count, name, os_version, device_family, type, auto_boot, session_id }) => {
      try {
        const body: Record<string, unknown> = { count };
        if (name) body.name = name;
        if (os_version) body.os_version = os_version;
        if (device_family) body.device_family = device_family;
        if (type) body.device_type = type;
        if (auto_boot !== undefined) body.auto_boot = auto_boot;
        if (session_id) body.session_id = session_id;

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
