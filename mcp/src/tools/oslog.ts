import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerOslogTools(server: McpServer): void {
  server.registerTool("start_oslog_streaming", {
    description: `Start streaming logs from the host Mac's unified logging system.

Captures os_log and Logger output from any process on the host Mac. Logs appear
in tail_logs/query_logs with source="oslog". Use subsystem and process filters
to limit noise — these are applied at the subprocess level (log stream --predicate)
for efficiency.

Useful for capturing logs from dev tools (Vite, webpack, etc.) that write to
os_log, or from any macOS process you want to observe alongside device logs.`,
    inputSchema: strictParams({
      subsystem: z
        .string()
        .optional()
        .describe("Filter by os_log subsystem (e.g. 'dev.quern.helm')"),
      process: z
        .string()
        .optional()
        .describe("Filter by process name (suffix match on processImagePath)"),
    }),
  }, async ({ subsystem, process }) => {
    try {
      const body: Record<string, unknown> = {};
      if (subsystem) body.subsystem = subsystem;
      if (process) body.process = process;

      const data = await apiRequest(
        "POST",
        "/api/v1/logs/oslog/start",
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

  server.registerTool("stop_oslog_streaming", {
    description: `Stop streaming logs from the host Mac's unified logging system.`,
    inputSchema: strictParams({}),
  }, async () => {
    try {
      const data = await apiRequest(
        "POST",
        "/api/v1/logs/oslog/stop",
        undefined,
        {}
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
