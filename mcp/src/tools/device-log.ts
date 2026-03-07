import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerDeviceLogTools(server: McpServer): void {
  server.registerTool("start_device_logging", {
    description: `Start capturing logs from a physical device via pymobiledevice3 syslog.

Captures os_log and Logger output. Logs appear in tail_logs/query_logs
with source="device". Use process or match filters to limit noise — these
are applied at the subprocess level (pymobiledevice3 -pn flag) for efficiency.

Use the preset parameter to apply an ingestion filter at start time (e.g.
"device-quiet" to exclude system daemons). For cleanest results, combine
process filter with set_log_filter subsystems include after starting:
  start_device_logging(process: "MyApp", preset: "device-quiet")
  set_log_filter(source: "device", process: "MyApp", subsystems: ["MyApp.debug.dylib"])

NOTE: This does NOT capture print() output — print() writes to stdout, not
the unified logging system. Use os.Logger in your app instead.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Device UDID (defaults to active device)"),
      process: z
        .string()
        .optional()
        .describe("Filter by process name (e.g. 'MyApp')"),
      match: z
        .string()
        .optional()
        .describe("Filter by message content substring"),
      preset: z
        .string()
        .optional()
        .describe("Apply an ingestion filter preset at start (e.g. 'device-quiet')"),
    }),
  }, async ({ udid, process, match, preset }) => {
    try {
      const body: Record<string, unknown> = {};
      if (udid) body.udid = udid;
      if (process) body.process = process;
      if (match) body.match = match;
      if (preset) body.preset = preset;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/logging/device/start",
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

  server.registerTool("stop_device_logging", {
    description: `Stop capturing logs from a physical device.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Device UDID (defaults to active device)"),
    }),
  }, async ({ udid }) => {
    try {
      const body: Record<string, unknown> = {};
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/logging/device/stop",
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
