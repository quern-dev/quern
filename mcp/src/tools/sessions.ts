import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { heldSessionState } from "../held-session.js";
import { SESSION_ID, apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

function answer(data: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
}

function failure(e: unknown) {
  return {
    content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
    isError: true,
  };
}

const path = (suffix = "") => `/api/v1/sessions/${encodeURIComponent(SESSION_ID)}${suffix}`;

export function registerSessionTools(server: McpServer): void {
  server.registerTool("reserve_devices", {
    description: `Reserve devices for this agent, so no other agent or script sharing the quern server can change them. While you hold a device, another caller's taps, installs and other changes to it are refused (423, naming you); reading it -- screenshots, the UI tree, logs -- still works for everyone. Use it whenever more than one agent or a CI job may share the server.

Give exactly one of: udids; count (optionally with device_type/os_version/name/device_family -- free devices are picked, booted first, booting more if auto_boot); or group (a named set in ~/.quern/config.json's device_groups, optionally with count). All or nothing: if any device is held by someone else, nothing is reserved and the error names the holder. Reserving exactly one device also makes it your active device.

Your reservation lasts as long as this MCP session: released when it ends, and kept for a grace period (default 3 minutes) if the connection to quern drops, so a network blip costs nothing. If quern restarts, reservations are lost and your next tool result will end with a 'quern session notice:' line saying so -- reserve again.`,
    inputSchema: strictParams({
      udids: z.array(z.string()).optional().describe("Exactly these devices"),
      count: z.number().int().min(1).max(64).optional().describe("How many devices to pick (default 1 with criteria)"),
      group: z.string().optional().describe("A named group from config.json's device_groups"),
      device_type: z.enum(["simulator", "device", "android_emulator", "android_device"]).optional(),
      os_version: z.string().optional().describe("OS version prefix, e.g. '18'"),
      name: z.string().optional().describe("Device name pattern, e.g. 'iPhone 16'"),
      device_family: z.string().optional().describe("'iPhone', 'iPad', ..."),
      auto_boot: z.boolean().optional().describe("Boot shutdown devices to make up the count (default true)"),
      on_release: z.enum(["none", "shutdown_booted"]).optional().describe("shutdown_booted: when released, shut down the simulators this reservation booted. Default none"),
    }),
  }, async (args) => {
    try {
      return answer(await apiRequest("POST", path("/reservations"), undefined, args));
    } catch (e) {
      return failure(e);
    }
  });

  server.registerTool("release_devices", {
    description: "Release devices this agent reserved with reserve_devices: one (udid) or all. Not required -- they are released when this session ends -- but frees them for others sooner.",
    inputSchema: strictParams({
      udid: z.string().optional().describe("Release just this device; omit for all"),
    }),
  }, async ({ udid }) => {
    try {
      return answer(await apiRequest("DELETE", path("/reservations"), udid ? { udid } : undefined));
    } catch (e) {
      return failure(e);
    }
  });

  server.registerTool("session_status", {
    description: "This agent's quern session: its id, whether its connection is live, and the devices it holds. Also lists every session on the server and who holds what, when all is true.",
    inputSchema: strictParams({
      all: z.boolean().optional().describe("List every session on the server, not just this one"),
    }),
  }, async ({ all }) => {
    const local = heldSessionState();
    try {
      if (all) {
        return answer({ you: local, ...(await apiRequest("GET", "/api/v1/sessions") as object) });
      }
      return answer({ you: local, session: await apiRequest("GET", path()) });
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      // Only a 404 means "no such session". Anything else -- a server that is
      // down, a refused key, a 500 -- is a failure to ask, and saying "no held
      // session" sent agents to reserve again instead of fixing that
      // (CodeRabbit).
      const note = !all && message.startsWith("HTTP 404")
        ? `the server has no held session ${SESSION_ID} right now`
        : `could not ask the quern server about sessions: ${message}`;
      return answer({ you: local, session: null, note });
    }
  });
}
