import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";

export function registerDeviceLogTools(server: McpServer): void {
  server.tool(
    "start_device_logging",
    `Start capturing logs from a physical device via pymobiledevice3 syslog.

Captures os_log, Logger, and NSLog output. Logs appear in tail_logs/query_logs
with source="device". Use process or match filters to limit noise.

NOTE: This does NOT capture print() output. For apps using print(), you need
an in-app log drain (freopen redirect).`,
    {
      udid: z
        .string()
        .optional()
        .describe("Device UDID (auto-resolves if omitted)"),
      process: z
        .string()
        .optional()
        .describe("Filter by process name (e.g. 'MyApp')"),
      match: z
        .string()
        .optional()
        .describe("Filter by message content substring"),
    },
    async ({ udid, process, match }) => {
      try {
        const body: Record<string, unknown> = {};
        if (udid) body.udid = udid;
        if (process) body.process = process;
        if (match) body.match = match;

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
    }
  );

  server.tool(
    "stop_device_logging",
    `Stop capturing logs from a physical device.`,
    {
      udid: z
        .string()
        .optional()
        .describe("Device UDID (auto-resolves if omitted)"),
    },
    async ({ udid }) => {
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
    }
  );
}
