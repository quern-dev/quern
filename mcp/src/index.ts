#!/usr/bin/env node

/**
 * Quern Debug Server — MCP Server
 *
 * Thin wrapper that translates MCP tool calls into HTTP requests
 * to the Python log server running on localhost:9100.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { probeServer } from "./http.js";
import { registerLogTools } from "./tools/logs.js";
import { registerProxyTools } from "./tools/proxy.js";
import { registerInterceptTools } from "./tools/intercept.js";
import { registerDeviceTools } from "./tools/device.js";
import { registerDevicePoolTools } from "./tools/device-pool.js";
import { registerSimulatorLogTools } from "./tools/simulator-log.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

const server = new McpServer({
  name: "quern-debug-server",
  version: "0.1.0",
});

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

registerLogTools(server);
registerProxyTools(server);
registerInterceptTools(server);
registerDeviceTools(server);
registerDevicePoolTools(server);
registerSimulatorLogTools(server);

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

function readResourceFile(filename: string): string {
  try {
    const filePath = join(__dirname, "..", "..", "docs", filename);
    return readFileSync(filePath, "utf-8");
  } catch (e) {
    return `Error: Could not read ${filename} — ${e instanceof Error ? e.message : String(e)}`;
  }
}

server.resource(
  "guide",
  "quern://guide",
  {
    description:
      "Agent guide: principles, workflows, tool selection, REST API reference, and performance tips",
    mimeType: "text/markdown",
  },
  async () => ({
    contents: [
      {
        uri: "quern://guide",
        mimeType: "text/markdown",
        text: readResourceFile("agent-guide.md"),
      },
    ],
  })
);

server.resource(
  "troubleshooting",
  "quern://troubleshooting",
  {
    description:
      "iOS error patterns, crash report reading guide, and debugging tips",
    mimeType: "text/markdown",
  },
  async () => ({
    contents: [
      {
        uri: "quern://troubleshooting",
        mimeType: "text/markdown",
        text: readResourceFile("troubleshooting.md"),
      },
    ],
  })
);

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  await probeServer();

  const transport = new StdioServerTransport();
  await server.connect(transport);

  console.error("Quern Debug MCP Server running on stdio");
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
