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
import { registerDeviceUITools } from "./tools/device-ui.js";
import { registerDevicePoolTools } from "./tools/device-pool.js";
import { registerSimulatorLogTools } from "./tools/simulator-log.js";
import { registerDeviceLogTools } from "./tools/device-log.js";
import { registerWdaTools } from "./tools/wda.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

const instructions = [
  "Quern is a debug server for AI-assisted iOS development — it captures logs, intercepts network traffic, and controls simulators/devices via MCP tools.",
  "",
  "SESSION START: resolve_device → get_screen_summary → proxy_status",
  "",
  "CORE PRINCIPLES:",
  "- Structured data over screenshots: use get_screen_summary and get_ui_tree for decisions, screenshots for visual verification",
  "- Accessibility over coordinates: use tap_element with label/element_type instead of tap with x,y",
  "- Summarize first, drill down second: start with get_log_summary, get_flow_summary, get_screen_summary — then filter",
  "- Verify state before acting: check proxy_status before capturing, check screen before tapping",
  "- Server-side waiting: use wait_for_element instead of polling get_ui_tree; use list_held_flows with timeout instead of polling",
  "- Filter aggressively: always filter logs by level/process/search, flows by host/method/status, UI by max_elements/children_of",
  "",
  "TOOL QUICK REFERENCE:",
  "- See screen: get_screen_summary (quick) | get_ui_tree (full) | take_screenshot (visual) | take_annotated_screenshot (a11y overlay)",
  "- Interact: tap_element (preferred) | tap (coordinates, rare) | swipe | type_text (clear_text first if field has content)",
  "- Network: get_flow_summary → query_flows → get_flow_detail | wait_for_flow (block until match) | set_mock (synthetic responses) | set_intercept + release_flow (modify live traffic)",
  "- Logs: get_log_summary → query_logs | tail_logs (recent) | get_errors | get_latest_crash",
  "- Devices: resolve_device (find/boot/claim) | install_app | launch_app | terminate_app | uninstall_app | list_apps | grant_permission (sim only)",
  "- Device selection: use list_devices or resolve_device to discover devices. Prefer already-booted simulators or connected physical devices unless the user specifies otherwise.",
  "",
  "NETWORK CAPTURE:",
  "- Local capture (recommended for simulators): transparent, per-simulator flow tagging via simulator_udid. Check proxy_status local_capture field.",
  "- System proxy: configure_system_proxy to start, unconfigure_system_proxy when done. Always unconfigure when finished.",
  "- If no flows captured: verify certs with verify_proxy_setup, fix with install_proxy_cert",
  "",
  "PHYSICAL DEVICES: Call setup_wda once for first-time setup. After that, WDA auto-starts on first interaction. Use start_device_logging / stop_device_logging for logs. get_latest_crash with udid for crash reports.",
  "",
  "TROUBLESHOOTING: If tools fail with connection errors, call ensure_server to check/restart the server.",
  "",
  "For the full agent guide with workflows, advanced patterns, and troubleshooting: read the quern://guide resource.",
].join("\n");

const server = new McpServer(
  {
    name: "quern-debug-server",
    version: "0.1.0",
  },
  { instructions },
);

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

registerLogTools(server);
registerProxyTools(server);
registerInterceptTools(server);
registerDeviceTools(server);
registerDeviceUITools(server);
registerDevicePoolTools(server);
registerSimulatorLogTools(server);
registerDeviceLogTools(server);
registerWdaTools(server);

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
