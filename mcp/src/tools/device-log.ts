import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerDeviceLogTools(server: McpServer): void {
  server.registerTool("start_device_logging", {
    description: `Start capturing logs from a physical iOS device or Android device/emulator.

iOS: Uses pymobiledevice3 syslog. Captures os_log/Logger output with source="device".
Android: Uses adb logcat. Captures logcat output with source="logcat".

Use process or match filters to limit noise. Use the preset parameter to
apply an ingestion filter at start time (e.g. "device-quiet" to exclude
system daemons).

NOTE (iOS): Does NOT capture print() — use os.Logger instead.`,
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
    description: `Stop capturing logs from a physical iOS or Android device.`,
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
