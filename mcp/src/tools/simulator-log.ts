import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerSimulatorLogTools(server: McpServer): void {
  server.registerTool("start_simulator_logging", {
    description: `Start capturing logs from a simulator app via unified logging.

Captures os_log, Logger, and NSLog output. Logs appear in tail_logs/query_logs
with source="simulator". Use process or subsystem filters to limit noise — these
are applied at the subprocess level (simctl log stream --predicate) for efficiency.

Use the preset parameter to apply an ingestion filter at start time (e.g.
"simulator-quiet"). For cleanest results, set both subprocess and ingestion filters:
  start_simulator_logging(process: "MyApp", subsystem: "com.myapp", preset: "simulator-quiet")

NOTE: This does NOT capture print() output — print() writes to stdout, not
the unified logging system. Use os.Logger in your app instead.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Simulator UDID (defaults to active device)"),
      process: z
        .string()
        .optional()
        .describe("Filter by process name (e.g. 'MyApp')"),
      subsystem: z
        .string()
        .optional()
        .describe("Filter by subsystem (e.g. 'com.example.app')"),
      level: z
        .enum(["debug", "info", "default", "error"])
        .optional()
        .describe("Minimum log level (default: debug)"),
      preset: z
        .string()
        .optional()
        .describe("Apply an ingestion filter preset at start (e.g. 'simulator-quiet')"),
    }),
  }, async ({ udid, process, subsystem, level, preset }) => {
    try {
      const body: Record<string, unknown> = {};
      if (udid) body.udid = udid;
      if (process) body.process = process;
      if (subsystem) body.subsystem = subsystem;
      if (level) body.level = level;
      if (preset) body.preset = preset;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/logging/start",
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

  server.registerTool("stop_simulator_logging", {
    description: `Stop capturing logs from a simulator.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .optional()
        .describe("Simulator UDID (defaults to active device)"),
    }),
  }, async ({ udid }) => {
    try {
      const body: Record<string, unknown> = {};
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/logging/stop",
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
