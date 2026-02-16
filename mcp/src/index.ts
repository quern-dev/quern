#!/usr/bin/env node

/**
 * Quern Debug Server — MCP Server
 *
 * Thin wrapper that translates MCP tool calls into HTTP requests
 * to the Python log server running on localhost:9100.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { z } from "zod";

// ---------------------------------------------------------------------------
// Configuration & State Discovery
// ---------------------------------------------------------------------------

const CONFIG_DIR = join(homedir(), ".quern");
const STATE_FILE = join(CONFIG_DIR, "state.json");
const API_KEY_FILE = join(CONFIG_DIR, "api-key");

interface ServerState {
  pid: number;
  server_port: number;
  proxy_port: number;
  proxy_enabled: boolean;
  proxy_status: string;
  started_at: string;
  api_key: string;
  active_devices: string[];
}

function readStateFile(): ServerState | null {
  try {
    if (!existsSync(STATE_FILE)) return null;
    const content = readFileSync(STATE_FILE, "utf-8").trim();
    if (!content) return null;
    return JSON.parse(content) as ServerState;
  } catch {
    return null;
  }
}

function discoverServer(): { url: string; apiKey: string } {
  // Priority 1: Environment variable
  if (process.env.QUERN_DEBUG_SERVER_URL) {
    return {
      url: process.env.QUERN_DEBUG_SERVER_URL,
      apiKey: loadApiKey(),
    };
  }

  // Priority 2: State file
  const state = readStateFile();
  if (state) {
    return {
      url: `http://127.0.0.1:${state.server_port}`,
      apiKey: state.api_key || loadApiKey(),
    };
  }

  // Priority 3: Default
  return {
    url: "http://127.0.0.1:9100",
    apiKey: loadApiKey(),
  };
}

function loadApiKey(): string {
  try {
    return readFileSync(API_KEY_FILE, "utf-8").trim();
  } catch {
    console.error(
      "WARNING: Could not read API key from ~/.quern/api-key"
    );
    return "";
  }
}

// Lazy-resolved at call time (not module load) so state.json updates are picked up
function getServerUrl(): string {
  return discoverServer().url;
}

function getApiKey(): string {
  return discoverServer().apiKey;
}

// Kept for backward compat in places that reference these directly
const SERVER_URL = process.env.QUERN_DEBUG_SERVER_URL || "http://127.0.0.1:9100";
const API_KEY = loadApiKey();

// ---------------------------------------------------------------------------
// HTTP helper
// ---------------------------------------------------------------------------

async function apiRequest(
  method: "GET" | "POST" | "DELETE",
  path: string,
  params?: Record<string, string | number | boolean | undefined>,
  body?: unknown
): Promise<unknown> {
  const server = discoverServer();
  const url = new URL(path, server.url);

  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null) {
        url.searchParams.set(k, String(v));
      }
    }
  }

  const headers: Record<string, string> = {
    Authorization: `Bearer ${server.apiKey}`,
  };

  const init: RequestInit = { method, headers };

  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  const resp = await fetch(url.toString(), init);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`HTTP ${resp.status}: ${text}`);
  }

  const text = await resp.text();
  if (!text) return null;
  return JSON.parse(text);
}

// ---------------------------------------------------------------------------
// Health probe
// ---------------------------------------------------------------------------

async function probeServer(): Promise<void> {
  const serverUrl = discoverServer().url;
  try {
    await fetch(new URL("/health", serverUrl).toString(), {
      signal: AbortSignal.timeout(3000),
    });
    console.error(`Connected to Quern Debug Server at ${serverUrl}`);
  } catch {
    console.error(
      `WARNING: Cannot reach Quern Debug Server at ${serverUrl} — use ensure_server tool to start it`
    );
  }
}

// ---------------------------------------------------------------------------
// MCP Server
// ---------------------------------------------------------------------------

const server = new McpServer({
  name: "quern-debug-server",
  version: "0.1.0",
});

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

server.tool(
  "ensure_server",
  `Ensure the Quern Debug Server is running. Reads state.json, health checks, and starts the server if needed. This is the recommended first tool call for any agent session. Returns connection info including server URL, proxy port, and API key.`,
  {},
  async () => {
    try {
      // Check if already running via state file
      const state = readStateFile();
      if (state) {
        // Try health check with retry logic
        const healthUrl = `http://127.0.0.1:${state.server_port}/health`;
        let lastError: Error | null = null;

        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            const resp = await fetch(healthUrl, {
              signal: AbortSignal.timeout(5000),
            });
            if (resp.ok) {
              return {
                content: [
                  {
                    type: "text" as const,
                    text: JSON.stringify(
                      {
                        status: "running",
                        server_url: `http://127.0.0.1:${state.server_port}`,
                        proxy_port: state.proxy_port,
                        proxy_enabled: state.proxy_enabled,
                        proxy_status: state.proxy_status,
                        api_key: state.api_key,
                        started_at: state.started_at,
                        pid: state.pid,
                      },
                      null,
                      2
                    ),
                  },
                ],
              };
            }
          } catch (e) {
            lastError = e instanceof Error ? e : new Error(String(e));
            if (attempt < 2) {
              // Wait before retry
              await new Promise(resolve => setTimeout(resolve, 500));
            }
          }
        }

        // Health check failed after retries - log details
        console.error(
          `Health check failed after 3 attempts to ${healthUrl}: ${lastError?.message || "Unknown error"}`
        );
        console.error(`State file indicates PID ${state.pid} should be running`);
        console.error("Will attempt to start server...");
      }

      // Try to start the server - check multiple locations
      const possibleCommands = [
        "quern-debug-server start",  // System PATH
        `${homedir()}/.local/bin/quern-debug-server start`,  // Common user install
        `${homedir()}/Dev/quern-debug-server/.venv/bin/python -m server.main start`,  // Dev environment
      ];

      let startError: Error | null = null;
      let commandTried = "";

      for (const cmd of possibleCommands) {
        try {
          console.error(`Trying to start server with: ${cmd}`);
          execSync(cmd, {
            timeout: 10000,
            stdio: "pipe",
          });
          commandTried = cmd;
          break;  // Success - exit loop
        } catch (e) {
          startError = e instanceof Error ? e : new Error(String(e));
          console.error(`Command failed: ${cmd} - ${startError.message}`);
          // Continue to next command
        }
      }

      // Check if server actually started (regardless of command success)
      const postState = readStateFile();
      if (!postState) {
        return {
          content: [
            {
              type: "text" as const,
              text: [
                "Error: Failed to start Quern Debug Server.",
                "",
                "Tried commands:",
                ...possibleCommands.map(cmd => `  - ${cmd}`),
                "",
                "Last error:",
                `  ${startError?.message || "Unknown error"}`,
                "",
                "Troubleshooting:",
                "1. Check if server is already running:",
                "   curl http://127.0.0.1:9100/health",
                "",
                "2. Try starting manually:",
                "   cd ~/Dev/quern-debug-server",
                "   .venv/bin/python -m server.main start",
                "",
                "3. Check logs:",
                "   tail -f ~/.quern/server.log",
              ].join("\n"),
            },
          ],
          isError: true,
        };
      }

      // Read freshly-written state and verify connectivity
      const newState = readStateFile();
      if (newState) {
        // Do a final health check to ensure it's actually reachable
        try {
          const verifyUrl = `http://127.0.0.1:${newState.server_port}/health`;
          const verifyResp = await fetch(verifyUrl, {
            signal: AbortSignal.timeout(5000),
          });

          if (!verifyResp.ok) {
            console.error(
              `Server started but health check failed: ${verifyResp.status} ${verifyResp.statusText}`
            );
          }
        } catch (e) {
          console.error(`Server started but not reachable: ${e instanceof Error ? e.message : String(e)}`);
          console.error("Server may still be starting up, or there may be a network issue");
        }

        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify(
                {
                  status: "started",
                  server_url: `http://127.0.0.1:${newState.server_port}`,
                  proxy_port: newState.proxy_port,
                  proxy_enabled: newState.proxy_enabled,
                  proxy_status: newState.proxy_status,
                  api_key: newState.api_key,
                  started_at: newState.started_at,
                  pid: newState.pid,
                },
                null,
                2
              ),
            },
          ],
        };
      }

      return {
        content: [
          {
            type: "text" as const,
            text: "Error: Server started but state file not found. Check ~/.quern/server.log for details.",
          },
        ],
        isError: true,
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
  "tail_logs",
  `Show recent log entries (most recent first). Use this for quick "what just happened?" queries. Defaults to the 50 most recent entries.`,
  {
    count: z
      .number()
      .min(1)
      .max(1000)
      .default(50)
      .describe("Number of recent entries to return"),
    level: z
      .enum(["debug", "info", "notice", "warning", "error", "fault"])
      .optional()
      .describe("Minimum log level filter"),
    process: z.string().optional().describe("Filter by process name"),
    source: z
      .enum(["syslog", "oslog", "crash", "build", "proxy", "app_drain"])
      .optional()
      .describe("Filter by log source"),
  },
  async ({ count, level, process, source }) => {
    try {
      const data = (await apiRequest("GET", "/api/v1/logs/query", {
        limit: count,
        level,
        process,
        source,
      })) as { entries: unknown[] };

      // Reverse to show most recent first
      const entries = [...(data.entries || [])].reverse();

      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify({ entries, total: entries.length }, null, 2),
          },
        ],
      };
    } catch (e) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "query_logs",
  `Full-featured log search with time ranges and text search. Use this for investigating specific issues — filter by time, process, level, or search text.`,
  {
    since: z
      .string()
      .optional()
      .describe("Start time (ISO 8601, e.g. 2026-02-08T10:00:00Z)"),
    until: z
      .string()
      .optional()
      .describe("End time (ISO 8601)"),
    level: z
      .enum(["debug", "info", "notice", "warning", "error", "fault"])
      .optional()
      .describe("Minimum log level"),
    process: z.string().optional().describe("Filter by process name"),
    source: z
      .enum(["syslog", "oslog", "crash", "build", "proxy", "app_drain"])
      .optional()
      .describe("Filter by log source"),
    search: z
      .string()
      .optional()
      .describe("Text search within log messages"),
    limit: z
      .number()
      .min(1)
      .max(1000)
      .default(100)
      .describe("Max entries to return"),
    offset: z.number().min(0).default(0).describe("Pagination offset"),
  },
  async ({ since, until, level, process, source, search, limit, offset }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/logs/query", {
        since,
        until,
        level,
        process,
        source,
        search,
        limit,
        offset,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "get_log_summary",
  `Get an AI-optimized summary of recent log activity. Returns error counts, top issues, and a natural language summary. Supports cursor-based polling for efficient delta updates.`,
  {
    window: z
      .enum(["30s", "1m", "5m", "15m", "1h"])
      .default("5m")
      .describe("Time window to summarize"),
    process: z.string().optional().describe("Filter to a specific process"),
    since_cursor: z
      .string()
      .optional()
      .describe(
        "Cursor from a previous summary response — returns only new activity since then"
      ),
  },
  async ({ window, process, since_cursor }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/logs/summary", {
        window,
        process,
        since_cursor,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "get_errors",
  `Get error-level log entries and crash reports. Useful for quickly finding what's going wrong.`,
  {
    since: z
      .string()
      .optional()
      .describe("Only errors after this time (ISO 8601)"),
    limit: z
      .number()
      .min(1)
      .max(1000)
      .default(50)
      .describe("Max entries to return"),
    include_crashes: z
      .boolean()
      .default(true)
      .describe("Include crash reports in results"),
  },
  async ({ since, limit, include_crashes }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/logs/errors", {
        since,
        limit,
        include_crashes,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "get_build_result",
  `Get the most recent parsed xcodebuild result, including errors, warnings, and test results.`,
  {},
  async () => {
    try {
      const data = await apiRequest("GET", "/api/v1/builds/latest");

      if (data === null) {
        return {
          content: [
            {
              type: "text" as const,
              text: "No build results yet. Submit build output via POST /api/v1/builds/parse first.",
            },
          ],
        };
      }

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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "get_latest_crash",
  `Get recent crash reports with parsed exception types, signals, and stack frames.`,
  {
    limit: z
      .number()
      .min(1)
      .max(100)
      .default(10)
      .describe("Max crash reports to return"),
    since: z
      .string()
      .optional()
      .describe("Only crashes after this time (ISO 8601)"),
  },
  async ({ limit, since }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/crashes/latest", {
        limit,
        since,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "set_log_filter",
  `Reconfigure log capture filters for a source adapter.`,
  {
    source: z.string().describe("Source adapter to configure (e.g. 'syslog')"),
    process: z
      .string()
      .optional()
      .describe("Filter to this process name"),
    exclude_patterns: z
      .array(z.string())
      .optional()
      .describe("Message patterns to exclude"),
  },
  async ({ source, process, exclude_patterns }) => {
    try {
      const data = await apiRequest("POST", "/api/v1/logs/filter", undefined, {
        source,
        process,
        exclude_patterns,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "query_flows",
  `Query captured HTTP flows from the network proxy. Filter by host, method, status code, and more.`,
  {
    host: z.string().optional().describe("Filter by hostname"),
    path_contains: z.string().optional().describe("Filter by path substring"),
    method: z
      .string()
      .optional()
      .describe("Filter by HTTP method (GET, POST, etc.)"),
    status_min: z
      .number()
      .optional()
      .describe("Minimum status code (e.g. 400 for errors)"),
    status_max: z
      .number()
      .optional()
      .describe("Maximum status code"),
    has_error: z
      .boolean()
      .optional()
      .describe("Filter to flows with connection errors"),
    limit: z
      .number()
      .min(1)
      .max(1000)
      .default(100)
      .describe("Max flows to return"),
    offset: z.number().min(0).default(0).describe("Pagination offset"),
  },
  async ({
    host,
    path_contains,
    method,
    status_min,
    status_max,
    has_error,
    limit,
    offset,
  }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/proxy/flows", {
        host,
        path_contains,
        method,
        status_min,
        status_max,
        has_error,
        limit,
        offset,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "get_flow_detail",
  `Get full request/response detail for a single captured HTTP flow, including headers and bodies.`,
  {
    flow_id: z.string().describe("The flow ID to retrieve"),
  },
  async ({ flow_id }) => {
    try {
      const data = await apiRequest(
        "GET",
        `/api/v1/proxy/flows/${encodeURIComponent(flow_id)}`
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "proxy_status",
  `Check proxy state and configuration. Returns status (running/stopped/error), port, flows captured, intercept state, mock rules count, and any errors.`,
  {},
  async () => {
    try {
      const data = await apiRequest("GET", "/api/v1/proxy/status");

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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "verify_proxy_setup",
  `Verify that mitmproxy CA certificate is installed on simulator(s). Performs ground-truth verification by querying the simulator's TrustStore database. Use this to check if proxy setup is complete before capturing traffic. Returns detailed installation status per device with timestamps.`,
  {
    udid: z
      .string()
      .optional()
      .describe(
        "Specific simulator UDID to verify. If omitted, verifies all booted simulators."
      ),
  },
  async ({ udid }) => {
    try {
      const body: Record<string, unknown> = {};
      if (udid !== undefined) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/proxy/cert/verify",
        undefined,
        Object.keys(body).length > 0 ? body : undefined
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "start_proxy",
  `Start the mitmproxy network capture. Automatically configures the macOS system proxy so simulator traffic is captured. Optionally specify port and listen host.`,
  {
    port: z
      .number()
      .optional()
      .describe("Port for the mitmproxy listener (default: 9101)"),
    listen_host: z
      .string()
      .optional()
      .describe("Host to listen on (default: 0.0.0.0)"),
    system_proxy: z
      .boolean()
      .optional()
      .describe(
        "Configure macOS system proxy automatically (default: true). Required for simulator traffic capture."
      ),
  },
  async ({ port, listen_host, system_proxy }) => {
    try {
      const body: Record<string, unknown> = {};
      if (port !== undefined) body.port = port;
      if (listen_host !== undefined) body.listen_host = listen_host;
      if (system_proxy !== undefined) body.system_proxy = system_proxy;

      const data = await apiRequest(
        "POST",
        "/api/v1/proxy/start",
        undefined,
        Object.keys(body).length > 0 ? body : undefined
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "stop_proxy",
  `Stop the mitmproxy network capture. Automatically restores the macOS system proxy to its pre-Quern state if it was configured.`,
  {},
  async () => {
    try {
      const data = await apiRequest("POST", "/api/v1/proxy/stop");

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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "get_flow_summary",
  `Get an LLM-optimized summary of recent HTTP traffic. Groups by host, shows errors, slow requests, and overall statistics. Supports cursor-based polling for efficient delta updates.`,
  {
    window: z
      .enum(["30s", "1m", "5m", "15m", "1h"])
      .default("5m")
      .describe("Time window to summarize"),
    host: z
      .string()
      .optional()
      .describe("Filter to a specific host"),
    since_cursor: z
      .string()
      .optional()
      .describe(
        "Cursor from a previous summary response — returns only new activity since then"
      ),
  },
  async ({ window, host, since_cursor }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/proxy/flows/summary", {
        window,
        host,
        since_cursor,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "proxy_setup_guide",
  `Get device proxy configuration instructions with auto-detected local IP. Includes steps for both simulator and physical device setup.`,
  {},
  async () => {
    try {
      const data = await apiRequest("GET", "/api/v1/proxy/setup-guide");

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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

server.tool(
  "configure_system_proxy",
  `Configure macOS system proxy to route traffic through mitmproxy. Required for simulator traffic capture. Auto-detects the active network interface. The proxy must be running first.`,
  {
    interface: z
      .string()
      .optional()
      .describe("Network interface name (e.g. 'Wi-Fi'). Auto-detected if omitted."),
  },
  async ({ interface: iface }) => {
    try {
      const body = iface ? { interface: iface } : undefined;
      const data = await apiRequest(
        "POST",
        "/api/v1/proxy/configure-system",
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
  "unconfigure_system_proxy",
  `Restore macOS system proxy to its pre-Quern state. Use when you want to stop routing traffic through mitmproxy but keep the proxy running.`,
  {},
  async () => {
    try {
      const data = await apiRequest(
        "POST",
        "/api/v1/proxy/unconfigure-system"
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
  "list_log_sources",
  `List all active log source adapters and their current status (streaming, watching, stopped, error).`,
  {},
  async () => {
    try {
      const data = await apiRequest("GET", "/api/v1/logs/sources");

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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the Quern Debug Server running? Start it with: quern-debug-server`,
          },
        ],
        isError: true,
      };
    }
  }
);

// ---------------------------------------------------------------------------
// Phase 2c: Intercept, Replay & Mock tools
// ---------------------------------------------------------------------------

server.tool(
  "set_intercept",
  `Set an intercept pattern on the proxy. Matching requests will be held (paused) until you release them. Uses mitmproxy filter syntax (e.g. "~d api.example.com", "~m POST & ~d api.example.com").`,
  {
    pattern: z
      .string()
      .describe(
        'mitmproxy filter pattern (e.g. "~d api.example.com", "~m POST & ~d api.example.com")'
      ),
  },
  async ({ pattern }) => {
    try {
      const data = await apiRequest(
        "POST",
        "/api/v1/proxy/intercept",
        undefined,
        { pattern }
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
  "clear_intercept",
  `Clear the intercept pattern and release all held flows. Flows will complete normally.`,
  {},
  async () => {
    try {
      const data = await apiRequest("DELETE", "/api/v1/proxy/intercept");

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
  "list_held_flows",
  `List flows currently held by the intercept filter. Supports long-polling: set timeout > 0 to block until a flow is intercepted or timeout expires. This is the recommended approach for MCP agents — make a single blocking call instead of rapid polling.`,
  {
    timeout: z
      .number()
      .min(0)
      .max(60)
      .default(0)
      .describe(
        "Long-poll timeout in seconds. 0 = return immediately. >0 = block until a flow is caught or timeout expires."
      ),
  },
  async ({ timeout }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/proxy/intercept/held", {
        timeout,
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
  "release_flow",
  `Release a held flow, optionally modifying the request before it continues. Modifications can include changes to headers, body, URL, or method.`,
  {
    flow_id: z.string().describe("The held flow ID to release"),
    modifications: z
      .object({
        headers: z
          .record(z.string())
          .optional()
          .describe("Headers to add/override"),
        body: z.string().optional().describe("New request body"),
        url: z.string().optional().describe("New request URL"),
        method: z.string().optional().describe("New HTTP method"),
      })
      .optional()
      .describe("Optional request modifications to apply before releasing"),
  },
  async ({ flow_id, modifications }) => {
    try {
      const data = await apiRequest(
        "POST",
        "/api/v1/proxy/intercept/release",
        undefined,
        { flow_id, modifications: modifications || null }
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
  "replay_flow",
  `Replay a previously captured HTTP flow through the proxy. The replayed request appears as a new flow in captures. Optionally modify headers or body.`,
  {
    flow_id: z.string().describe("The captured flow ID to replay"),
    modify_headers: z
      .record(z.string())
      .optional()
      .describe("Headers to add/override on the replayed request"),
    modify_body: z
      .string()
      .optional()
      .describe("New body for the replayed request"),
  },
  async ({ flow_id, modify_headers, modify_body }) => {
    try {
      const body: Record<string, unknown> = {};
      if (modify_headers) body.modify_headers = modify_headers;
      if (modify_body !== undefined) body.modify_body = modify_body;

      const data = await apiRequest(
        "POST",
        `/api/v1/proxy/replay/${encodeURIComponent(flow_id)}`,
        undefined,
        Object.keys(body).length > 0 ? body : undefined
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
  "set_mock",
  `Add a mock response rule. Requests matching the pattern will receive a synthetic response instead of hitting the real server. Mock rules take priority over intercept. Uses mitmproxy filter syntax.`,
  {
    pattern: z
      .string()
      .describe('mitmproxy filter pattern (e.g. "~d api.example.com & ~m POST")'),
    status_code: z
      .number()
      .default(200)
      .describe("HTTP status code for the mock response"),
    headers: z
      .record(z.string())
      .optional()
      .describe(
        'Response headers (default: {"content-type": "application/json"})'
      ),
    body: z
      .string()
      .default("")
      .describe("Response body string"),
  },
  async ({ pattern, status_code, headers, body }) => {
    try {
      const response: Record<string, unknown> = {
        status_code,
        body,
      };
      if (headers) {
        response.headers = headers;
      }

      const data = await apiRequest(
        "POST",
        "/api/v1/proxy/mocks",
        undefined,
        { pattern, response }
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
  "list_mocks",
  `List all active mock response rules.`,
  {},
  async () => {
    try {
      const data = await apiRequest("GET", "/api/v1/proxy/mocks");

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
  "clear_mocks",
  `Clear mock response rules. If rule_id is provided, removes only that rule. Otherwise removes all mock rules.`,
  {
    rule_id: z
      .string()
      .optional()
      .describe("Specific mock rule ID to remove. Omit to clear all."),
  },
  async ({ rule_id }) => {
    try {
      let data;
      if (rule_id) {
        data = await apiRequest(
          "DELETE",
          `/api/v1/proxy/mocks/${encodeURIComponent(rule_id)}`
        );
      } else {
        data = await apiRequest("DELETE", "/api/v1/proxy/mocks");
      }

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

// ---------------------------------------------------------------------------
// Phase 3: Device Control tools
// ---------------------------------------------------------------------------

server.tool(
  "list_devices",
  `List available iOS simulators and tool availability (simctl, idb). Returns device UDIDs, names, states, and OS versions.`,
  {
    state: z
      .enum(["booted", "shutdown"])
      .optional()
      .describe("Filter by device state"),
    type: z
      .enum(["simulator", "device"])
      .optional()
      .describe("Filter by device type"),
  },
  async ({ state, type }) => {
    try {
      const data = (await apiRequest("GET", "/api/v1/device/list")) as {
        devices: Array<Record<string, unknown>>;
        tools: Record<string, boolean>;
        active_udid: string | null;
      };

      let devices = data.devices;
      if (state) {
        devices = devices.filter((d) => d.state === state);
      }
      if (type) {
        devices = devices.filter((d) => d.device_type === type);
      }

      return {
        content: [
          {
            type: "text" as const,
            text: JSON.stringify(
              { devices, tools: data.tools, active_udid: data.active_udid },
              null,
              2
            ),
          },
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
  "boot_device",
  `Boot an iOS simulator by UDID or name.`,
  {
    udid: z.string().optional().describe("Device UDID to boot"),
    name: z
      .string()
      .optional()
      .describe('Device name to boot (e.g. "iPhone 16 Pro")'),
  },
  async ({ udid, name }) => {
    try {
      const body: Record<string, unknown> = {};
      if (udid) body.udid = udid;
      if (name) body.name = name;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/boot",
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
  "shutdown_device",
  `Shutdown an iOS simulator.`,
  {
    udid: z.string().describe("Device UDID to shutdown"),
  },
  async ({ udid }) => {
    try {
      const data = await apiRequest(
        "POST",
        "/api/v1/device/shutdown",
        undefined,
        { udid }
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
  "install_app",
  `Install an app (.app or .ipa) on a simulator.`,
  {
    app_path: z.string().describe("Path to the .app or .ipa file"),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ app_path, udid }) => {
    try {
      const body: Record<string, unknown> = { app_path };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/app/install",
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
  "launch_app",
  `Launch an app by bundle ID on a simulator.`,
  {
    bundle_id: z.string().describe("App bundle identifier (e.g. com.example.MyApp)"),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ bundle_id, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/app/launch",
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
  "terminate_app",
  `Terminate a running app by bundle ID.`,
  {
    bundle_id: z.string().describe("App bundle identifier"),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ bundle_id, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/app/terminate",
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
  "list_apps",
  `List installed apps on a simulator.`,
  {
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ udid }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/app/list", {
        udid,
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
  "take_screenshot",
  `Capture a screenshot from the simulator. Returns the image as base64-encoded data.`,
  {
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
    format: z
      .enum(["png", "jpeg"])
      .default("png")
      .describe("Image format"),
    scale: z
      .number()
      .min(0.1)
      .max(1.0)
      .default(0.5)
      .describe("Scale factor (0.1-1.0, default 0.5)"),
    quality: z
      .number()
      .min(1)
      .max(100)
      .default(85)
      .describe("JPEG quality (1-100, ignored for PNG)"),
  },
  async ({ udid, format, scale, quality }) => {
    try {
      const server = discoverServer();
      const url = new URL("/api/v1/device/screenshot", server.url);
      if (udid) url.searchParams.set("udid", udid);
      url.searchParams.set("format", format);
      url.searchParams.set("scale", String(scale));
      url.searchParams.set("quality", String(quality));

      const resp = await fetch(url.toString(), {
        headers: { Authorization: `Bearer ${server.apiKey}` },
      });

      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`HTTP ${resp.status}: ${text}`);
      }

      const buffer = Buffer.from(await resp.arrayBuffer());
      return {
        content: [
          {
            type: "image" as const,
            data: buffer.toString("base64"),
            mimeType:
              resp.headers.get("content-type") || "image/png",
          },
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
  "get_ui_tree",
  `Get the full accessibility tree (all UI elements) from the current screen. Optionally scope to children of a specific element using children_of. Requires idb.`,
  {
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
    children_of: z
      .string()
      .optional()
      .describe("Only return children of the element with this identifier or label"),
  },
  async ({ udid, children_of }) => {
    try {
      const params: Record<string, string> = {};
      if (udid) params.udid = udid;
      if (children_of) params.children_of = children_of;
      const data = await apiRequest("GET", "/api/v1/device/ui", params);

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
  "get_element_state",
  `Get a single element's state without fetching the entire UI tree. More efficient than get_ui_tree when you only need to check one element. Returns the element with its current state (enabled, value, etc.). If multiple elements match, returns the first with a match_count field. Requires idb.`,
  {
    label: z
      .string()
      .optional()
      .describe("Element label (case-insensitive)"),
    identifier: z
      .string()
      .optional()
      .describe("Element identifier (case-sensitive)"),
    element_type: z
      .string()
      .optional()
      .describe("Element type to narrow results (e.g., 'Button', 'TextField')"),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ label, identifier, element_type, udid }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/ui/element", {
        label,
        identifier,
        type: element_type,
        udid,
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
  "wait_for_element",
  `Wait for an element to satisfy a condition (server-side polling). Eliminates client-side retry loops and reduces API round-trips. Always returns with matched:true/false - timeouts are not errors. Supports conditions: exists, not_exists, visible, enabled, disabled, value_equals, value_contains. Requires idb.`,
  {
    label: z
      .string()
      .optional()
      .describe("Element label (case-insensitive)"),
    identifier: z
      .string()
      .optional()
      .describe("Element identifier (case-sensitive)"),
    element_type: z
      .string()
      .optional()
      .describe("Element type to narrow results (e.g., 'Button', 'TextField')"),
    condition: z
      .enum([
        "exists",
        "not_exists",
        "visible",
        "enabled",
        "disabled",
        "value_equals",
        "value_contains",
      ])
      .describe("Condition to wait for"),
    value: z
      .string()
      .optional()
      .describe("Required for value_equals and value_contains conditions"),
    timeout: z
      .number()
      .default(10)
      .describe("Max wait time in seconds (default 10, max 60)"),
    interval: z
      .number()
      .default(0.5)
      .describe("Poll interval in seconds (default 0.5)"),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({
    label,
    identifier,
    element_type,
    condition,
    value,
    timeout,
    interval,
    udid,
  }) => {
    try {
      const body: Record<string, unknown> = {
        condition,
        timeout,
        interval,
      };

      if (label !== undefined) body.label = label;
      if (identifier !== undefined) body.identifier = identifier;
      if (element_type !== undefined) body.type = element_type;
      if (value !== undefined) body.value = value;
      if (udid !== undefined) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/wait-for-element",
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
  "get_screen_summary",
  `Get an LLM-optimized text description of the current screen, including interactive elements and their locations. Uses smart truncation with prioritization (buttons with identifiers > form inputs > generic buttons > static text). Navigation chrome (tab bars, nav bars) is always included regardless of limit. Requires idb.`,
  {
    max_elements: z
      .number()
      .default(20)
      .describe("Maximum interactive elements to include (0 = unlimited, default 20)"),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ max_elements, udid }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/screen-summary", {
        max_elements,
        udid,
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
  "tap",
  `Tap at specific screen coordinates on the simulator. Requires idb.`,
  {
    x: z.number().describe("X coordinate"),
    y: z.number().describe("Y coordinate"),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ x, y, udid }) => {
    try {
      const body: Record<string, unknown> = { x, y };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/tap",
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
  "tap_element",
  `Find a UI element by label or accessibility identifier and tap its center. Returns "ambiguous" with match list if multiple elements match — use element_type (e.g., "Button", "TextField", "StaticText") to narrow results. Requires idb.`,
  {
    label: z
      .string()
      .optional()
      .describe("Element label text to search for"),
    identifier: z
      .string()
      .optional()
      .describe("Accessibility identifier to search for"),
    element_type: z
      .string()
      .optional()
      .describe('Element type to filter by (e.g. "Button", "TextField")'),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ label, identifier, element_type, udid }) => {
    try {
      const body: Record<string, unknown> = {};
      if (label) body.label = label;
      if (identifier) body.identifier = identifier;
      if (element_type) body.element_type = element_type;
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/tap-element",
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
  "swipe",
  `Perform a swipe gesture from one point to another. Requires idb.`,
  {
    start_x: z.number().describe("Starting X coordinate"),
    start_y: z.number().describe("Starting Y coordinate"),
    end_x: z.number().describe("Ending X coordinate"),
    end_y: z.number().describe("Ending Y coordinate"),
    duration: z
      .number()
      .default(0.5)
      .describe("Swipe duration in seconds (default 0.5)"),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ start_x, start_y, end_x, end_y, duration, udid }) => {
    try {
      const body: Record<string, unknown> = {
        start_x,
        start_y,
        end_x,
        end_y,
        duration,
      };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/swipe",
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
  "type_text",
  `Type text into the currently focused input field. Requires idb.`,
  {
    text: z.string().describe("Text to type"),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ text, udid }) => {
    try {
      const body: Record<string, unknown> = { text };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/type",
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
  "clear_text",
  `Clear all text in the currently focused input field (select-all + delete). Use this before type_text when a field has pre-existing content you want to replace. Note: Secure text fields (passwords) may not support select-all. Requires idb.`,
  {
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ udid }) => {
    try {
      const body: Record<string, unknown> = {};
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/clear",
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
  "press_button",
  `Press a hardware button on the simulator (e.g. HOME, LOCK, SIRI, APPLE_PAY). Requires idb.`,
  {
    button: z
      .string()
      .describe(
        "Button name (HOME, LOCK, SIDE_BUTTON, SIRI, APPLE_PAY)"
      ),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ button, udid }) => {
    try {
      const body: Record<string, unknown> = { button };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/ui/press",
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
  "set_location",
  `Set the simulated GPS location on a simulator.`,
  {
    latitude: z.number().describe("GPS latitude"),
    longitude: z.number().describe("GPS longitude"),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ latitude, longitude, udid }) => {
    try {
      const body: Record<string, unknown> = { latitude, longitude };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/location",
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
  "grant_permission",
  `Grant an app permission on a simulator (e.g. photos, camera, location, contacts, calendar, microphone, notifications).`,
  {
    bundle_id: z.string().describe("App bundle identifier"),
    permission: z
      .string()
      .describe(
        "Permission to grant (photos, camera, location, contacts, calendar, microphone, notifications, etc.)"
      ),
    udid: z
      .string()
      .optional()
      .describe("Target device UDID (auto-resolves if omitted)"),
  },
  async ({ bundle_id, permission, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id, permission };
      if (udid) body.udid = udid;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/permission",
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

// ---------------------------------------------------------------------------
// Device Pool Tools (Phase 4b-alpha)
// ---------------------------------------------------------------------------

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
  },
  async ({ state, claimed }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/devices/pool", {
        state,
        claimed,
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
  },
  async ({ session_id, udid, name }) => {
    try {
      const body: Record<string, unknown> = { session_id };
      if (udid) body.udid = udid;
      if (name) body.name = name;

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
      .describe("Device name pattern (e.g., 'iPhone 16 Pro')"),
    os_version: z
      .string()
      .optional()
      .describe("OS version prefix — '18' matches 18.x, '18.2' matches 18.2 exactly. Accepts both '18.2' and 'iOS 18.2'."),
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
      .describe("Claim the device for this session"),
  },
  async ({ name, os_version, auto_boot, wait_if_busy, wait_timeout, session_id }) => {
    try {
      const body: Record<string, unknown> = {};
      if (name) body.name = name;
      if (os_version) body.os_version = os_version;
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
      .describe("Device name pattern (e.g., 'iPhone 16 Pro')"),
    os_version: z
      .string()
      .optional()
      .describe("OS version prefix — '18' matches 18.x, '18.2' matches 18.2 exactly. Accepts both '18.2' and 'iOS 18.2'."),
    auto_boot: z
      .boolean()
      .optional()
      .default(true)
      .describe("Boot shutdown devices if not enough booted ones"),
    session_id: z
      .string()
      .optional()
      .describe("Claim all devices for this session"),
  },
  async ({ count, name, os_version, auto_boot, session_id }) => {
    try {
      const body: Record<string, unknown> = { count };
      if (name) body.name = name;
      if (os_version) body.os_version = os_version;
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

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------

server.resource(
  "guide",
  "logserver://guide",
  {
    description:
      "Tool selection guide, recommended workflows, and cursor-based polling patterns",
    mimeType: "text/markdown",
  },
  async () => ({
    contents: [
      {
        uri: "logserver://guide",
        mimeType: "text/markdown",
        text: GUIDE_CONTENT,
      },
    ],
  })
);

server.resource(
  "troubleshooting",
  "logserver://troubleshooting",
  {
    description:
      "Common iOS error patterns, crash report reading guide, and debugging tips",
    mimeType: "text/markdown",
  },
  async () => ({
    contents: [
      {
        uri: "logserver://troubleshooting",
        mimeType: "text/markdown",
        text: TROUBLESHOOTING_CONTENT,
      },
    ],
  })
);

// ---------------------------------------------------------------------------
// Resource content
// ---------------------------------------------------------------------------

const GUIDE_CONTENT = `# Quern Debug Server — Tool Selection Guide

## Getting Started

**Always call \`ensure_server\` first.** This tool checks if the server is running and starts it if needed. It returns the server URL, API key, and proxy port — everything you need to connect. You don't need to start the server manually.

## Quick Reference

| I want to…                        | Use this tool        |
|-----------------------------------|----------------------|
| Start/check the server            | \`ensure_server\`      |
| See what just happened            | \`tail_logs\`          |
| Search for specific errors        | \`query_logs\`         |
| Get an AI-friendly status update  | \`get_log_summary\`    |
| See only errors and crashes       | \`get_errors\`         |
| Check build results               | \`get_build_result\`   |
| Investigate a crash               | \`get_latest_crash\`   |
| Change what's being captured      | \`set_log_filter\`     |
| See captured HTTP traffic         | \`query_flows\`        |
| Inspect a specific HTTP flow      | \`get_flow_detail\`    |
| Get HTTP traffic summary          | \`get_flow_summary\`   |
| Check proxy status                | \`proxy_status\`       |
| Start/stop the proxy              | \`start_proxy\` / \`stop_proxy\` |
| Set up a device for proxying      | \`proxy_setup_guide\`  |
| Verify proxy certificate install  | \`verify_proxy_setup\` |
| Check which sources are active    | \`list_log_sources\`   |
| List simulators                   | \`list_devices\`       |
| Boot/shutdown simulator           | \`boot_device\` / \`shutdown_device\` |
| Install/launch/terminate app      | \`install_app\` / \`launch_app\` / \`terminate_app\` |
| List installed apps               | \`list_apps\`          |
| Take a screenshot                 | \`take_screenshot\`    |
| See what's on screen              | \`get_screen_summary\` / \`get_ui_tree\` |
| Tap an element by label           | \`tap_element\`        |
| Tap at coordinates                | \`tap\`                |
| Swipe gesture                     | \`swipe\`              |
| Type text                         | \`type_text\`          |
| Clear text in a field             | \`clear_text\`         |
| Press hardware button             | \`press_button\`       |
| Set GPS location                  | \`set_location\`       |
| Grant app permission              | \`grant_permission\`   |
| Intercept matching requests       | \`set_intercept\`      |
| Stop intercepting                 | \`clear_intercept\`    |
| See held/intercepted flows        | \`list_held_flows\`    |
| Release a held flow               | \`release_flow\`       |
| Replay a captured request         | \`replay_flow\`        |
| Mock an API response              | \`set_mock\`           |
| List active mock rules            | \`list_mocks\`         |
| Remove mock rules                 | \`clear_mocks\`        |

## REST API Path Reference

When calling the HTTP API directly (without MCP), use these paths:

| MCP Tool             | HTTP Method | REST Path                              |
|----------------------|-------------|----------------------------------------|
| \`ensure_server\`      | GET         | \`/health\`                              |
| \`tail_logs\`          | GET         | \`/api/v1/logs/query\`                   |
| \`query_logs\`         | GET         | \`/api/v1/logs/query\`                   |
| \`get_log_summary\`    | GET         | \`/api/v1/logs/summary\`                 |
| \`get_errors\`         | GET         | \`/api/v1/logs/errors\`                  |
| \`get_build_result\`   | GET         | \`/api/v1/builds/latest\`                |
| \`get_latest_crash\`   | GET         | \`/api/v1/crashes/latest\`               |
| \`set_log_filter\`     | POST        | \`/api/v1/logs/filter\`                  |
| \`list_log_sources\`   | GET         | \`/api/v1/logs/sources\`                 |
| \`query_flows\`        | GET         | \`/api/v1/proxy/flows\`                  |
| \`get_flow_detail\`    | GET         | \`/api/v1/proxy/flows/{id}\`             |
| \`get_flow_summary\`   | GET         | \`/api/v1/proxy/flows/summary\`          |
| \`proxy_status\`       | GET         | \`/api/v1/proxy/status\`                 |
| \`verify_proxy_setup\` | POST        | \`/api/v1/proxy/cert/verify\`            |
| \`start_proxy\`        | POST        | \`/api/v1/proxy/start\`                  |
| \`stop_proxy\`         | POST        | \`/api/v1/proxy/stop\`                   |
| \`set_intercept\`      | POST        | \`/api/v1/proxy/intercept\`              |
| \`clear_intercept\`    | DELETE      | \`/api/v1/proxy/intercept\`              |
| \`list_held_flows\`    | GET         | \`/api/v1/proxy/intercept/held\`         |
| \`release_flow\`       | POST        | \`/api/v1/proxy/intercept/release\`      |
| \`replay_flow\`        | POST        | \`/api/v1/proxy/replay/{id}\`            |
| \`set_mock\`           | POST        | \`/api/v1/proxy/mock\`                   |
| \`list_mocks\`         | GET         | \`/api/v1/proxy/mock\`                   |
| \`clear_mocks\`        | DELETE      | \`/api/v1/proxy/mock\`                   |
| \`list_devices\`       | GET         | \`/api/v1/device/list\`                  |
| \`boot_device\`        | POST        | \`/api/v1/device/boot\`                  |
| \`shutdown_device\`    | POST        | \`/api/v1/device/shutdown\`              |
| \`install_app\`        | POST        | \`/api/v1/device/app/install\`           |
| \`launch_app\`         | POST        | \`/api/v1/device/app/launch\`            |
| \`terminate_app\`      | POST        | \`/api/v1/device/app/terminate\`         |
| \`list_apps\`          | GET         | \`/api/v1/device/app/list\`              |
| \`take_screenshot\`    | GET         | \`/api/v1/device/screenshot\`            |
| \`get_ui_tree\`        | GET         | \`/api/v1/device/ui\`                    |
| \`get_element_state\`  | GET         | \`/api/v1/device/ui/element\`            |
| \`wait_for_element\`   | POST        | \`/api/v1/device/ui/wait-for-element\`   |
| \`get_screen_summary\` | GET         | \`/api/v1/device/screen-summary\`        |
| \`tap\`                | POST        | \`/api/v1/device/ui/tap\`                |
| \`tap_element\`        | POST        | \`/api/v1/device/ui/tap-element\`        |
| \`swipe\`              | POST        | \`/api/v1/device/ui/swipe\`              |
| \`type_text\`          | POST        | \`/api/v1/device/ui/type\`               |
| \`clear_text\`         | POST        | \`/api/v1/device/ui/clear\`              |
| \`press_button\`       | POST        | \`/api/v1/device/ui/press\`              |
| \`set_location\`       | POST        | \`/api/v1/device/location\`              |
| \`grant_permission\`   | POST        | \`/api/v1/device/permission\`            |
| \`list_device_pool\`   | GET         | \`/api/v1/devices/pool\`                 |
| \`claim_device\`       | POST        | \`/api/v1/devices/claim\`                |
| \`release_device\`     | POST        | \`/api/v1/devices/release\`              |
| \`resolve_device\`     | POST        | \`/api/v1/devices/resolve\`              |
| \`ensure_devices\`     | POST        | \`/api/v1/devices/ensure\`               |

## Recommended Workflows

### 1. Continuous Monitoring (Cursor-Based Polling)

Use \`get_log_summary\` with cursor support for efficient polling:

1. Call \`get_log_summary\` with \`window: "5m"\`
2. Save the \`cursor\` from the response
3. On next check, pass \`since_cursor\` — you'll only get new activity
4. Repeat — each call is a lightweight delta

This is the most token-efficient way to stay informed.

### 2. Investigating a Crash

1. \`get_latest_crash\` — see the crash type, signal, and top frames
2. \`get_errors\` with \`since\` set to just before the crash — see what led up to it
3. \`query_logs\` with \`process\` filter — get the full log trail for the crashed process

### 3. After a Build

1. \`get_build_result\` — see errors, warnings, and test results
2. If tests failed, \`query_logs\` with \`source: "build"\` for full output

### 4. Investigating Network Issues

1. \`query_flows\` with \`status_min: 400\` — see all failed HTTP requests
2. \`get_flow_detail\` with the flow ID — inspect full headers and body
3. \`query_flows\` with \`host\` filter — narrow to a specific API
4. \`query_logs\` with \`source: "proxy"\` — see network events in the log timeline

### 5. Proxy Control & Monitoring

1. \`proxy_status\` — check if the proxy is running and how many flows are captured
2. \`start_proxy\` — start the proxy (optionally on a custom port)
3. \`proxy_setup_guide\` — get device setup instructions with auto-detected local IP
4. \`verify_proxy_setup\` — verify CA certificate is installed on simulators (ground-truth SQLite check)
5. \`get_flow_summary\` with \`window: "5m"\` — get a traffic digest
6. Save the \`cursor\` and use \`since_cursor\` on subsequent calls for efficient delta polling
7. \`stop_proxy\` — stop the proxy when done

**Certificate Verification Workflow:**
- After setting up the proxy, use \`verify_proxy_setup\` to confirm the mitmproxy CA certificate is installed
- This performs a ground-truth check by querying the simulator's TrustStore database
- Returns detailed status per device with installation timestamps
- If cert is missing, install it with: \`xcrun simctl keychain <udid> add-root-cert ~/.mitmproxy/mitmproxy-ca-cert.pem\`

### 6. Intercepting & Modifying Requests

Use intercept to pause matching requests, inspect them, and optionally modify before releasing:

1. \`set_intercept\` with pattern (e.g. \`"~d api.example.com & ~m POST"\`)
2. \`list_held_flows\` with \`timeout: 30\` — long-poll blocks until a flow is caught
3. Inspect the held flow's request details
4. \`release_flow\` with the flow ID — optionally pass \`modifications\` to change headers, body, URL, or method
5. \`clear_intercept\` when done — releases all remaining held flows

**Important**: Held flows auto-release after 30 seconds to prevent hanging clients.

### 7. Mocking API Responses

Use mocks to return synthetic responses without hitting the real server:

1. \`set_mock\` with a pattern and response spec (status code, headers, body)
2. Matching requests immediately get the mock response — they appear in flow captures as "MOCK" entries
3. \`list_mocks\` to see active rules
4. \`clear_mocks\` to remove rules (specific by rule_id, or all)

**Note**: Mock rules take priority over intercept — if a request matches both a mock and an intercept pattern, the mock wins.

### 8. Replaying Requests

Replay a previously captured flow to reproduce behavior:

1. \`query_flows\` to find the flow you want to replay
2. \`replay_flow\` with the flow ID — optionally modify headers or body
3. The replayed request goes through the proxy, so it appears as a new captured flow

### 9. Device Control Workflow

Use device tools to inspect and interact with the simulator:

1. \`resolve_device\` — find the best available device matching your criteria (preferred over manual listing)
2. \`boot_device\` — boot a simulator if needed (or use \`resolve_device\` with \`auto_boot: true\`)
3. \`install_app\` / \`launch_app\` — deploy and start the app
4. \`get_screen_summary\` — understand what's on screen (text description)
5. \`take_screenshot\` — see the actual screen image
6. \`tap_element\` — interact with UI elements by label/identifier
7. \`type_text\` — enter text into focused fields
8. \`clear_text\` — clear a pre-filled text field before typing new content
9. \`get_screen_summary\` — verify the result

**Tip**: \`tap_element\` is preferred over \`tap\` (coordinates) because it finds
elements by label, handling layout differences. If multiple elements match, it
returns "ambiguous" with a list — narrow by \`element_type\` or \`identifier\`.

**Tip**: For parallel testing, use \`ensure_devices\` to boot and claim N devices,
then pass each device's \`udid\` to subsequent tool calls for isolation.

**Tool requirements**: Device management and screenshots use \`simctl\` (always available with Xcode). UI inspection and interaction (\`get_ui_tree\`, \`tap\`, \`swipe\`, \`type_text\`, \`clear_text\`, \`press_button\`) require \`idb\`. Check \`list_devices\` response for tool availability.

### 10. Debugging a Specific Issue

1. \`query_logs\` with \`search\` to find relevant messages
2. \`query_logs\` with \`process\` and time range to narrow down
3. \`get_log_summary\` for the big picture

## Tips

- **tail_logs vs query_logs**: Use \`tail_logs\` for "show me recent stuff" (defaults to 50, newest first). Use \`query_logs\` for searching with filters and time ranges.
- **Level filtering**: \`level: "error"\` returns ERROR and FAULT entries.
- **Sources**: \`syslog\` = device system log, \`oslog\` = macOS unified log, \`crash\` = crash reports, \`build\` = xcodebuild output, \`proxy\` = network traffic.
- **Long-polling**: \`list_held_flows\` with \`timeout: 30\` is more efficient than polling every second — one blocking call instead of 30 rapid calls.
- **Mock vs Intercept**: Mocks return instant synthetic responses. Intercept pauses real requests for inspection. Use mocks for stable test fixtures, intercept for ad-hoc debugging.
`;

const TROUBLESHOOTING_CONTENT = `# Quern Debug Server — Troubleshooting Guide

## Common iOS Error Patterns

### Sandbox Violations
\`\`\`
Sandbox: MyApp(1234) deny(1) file-read-data /path/to/file
\`\`\`
**Cause**: App is trying to access a file outside its sandbox.
**Fix**: Check entitlements and file access patterns. Use proper APIs (FileManager, UIDocumentPickerViewController).

### AMFI / Code Signing
\`\`\`
AMFI: code signature validation failed
\`\`\`
**Cause**: Code signature is invalid or missing.
**Fix**: Clean build folder, re-sign the app, check provisioning profiles.

### AutoLayout Constraint Conflicts
\`\`\`
Unable to simultaneously satisfy constraints
\`\`\`
**Cause**: Conflicting layout constraints.
**Fix**: Look for the constraint dump in the log. Set \`translatesAutoresizingMaskIntoConstraints = false\`. Use constraint priorities.

### Memory Warnings
\`\`\`
Received memory warning
\`\`\`
**Cause**: App is using too much memory.
**Fix**: Profile with Instruments (Leaks, Allocations). Check for retain cycles, large image buffers, or unbounded caches.

### Network / TLS Issues
\`\`\`
NSURLSession/NSURLConnection HTTP load failed
TIC TCP Conn Failed
boringssl_context_error_print
\`\`\`
**Cause**: Network request failed, often due to ATS or certificate issues.
**Fix**: Check App Transport Security settings. Verify server certificates. Check network connectivity.

### CoreData
\`\`\`
CoreData: error: Failed to call designated initializer
\`\`\`
**Cause**: CoreData model/migration issue.
**Fix**: Check data model version, migration mappings, and entity class names.

## Reading Crash Reports

### Key Fields

- **Exception Type**: The Mach exception (e.g., \`EXC_BAD_ACCESS\`, \`EXC_CRASH\`)
- **Exception Codes**: Specific error codes (e.g., \`KERN_INVALID_ADDRESS at 0x0\`)
- **Signal**: Unix signal (\`SIGSEGV\` = bad memory access, \`SIGABRT\` = abort, \`SIGTRAP\` = breakpoint/assertion)
- **Faulting Thread**: The thread that crashed — look at its stack frames

### Common Crash Types

| Exception | Signal | Meaning |
|-----------|--------|---------|
| EXC_BAD_ACCESS | SIGSEGV | Dereferenced bad pointer (null, dangling, wild) |
| EXC_BAD_ACCESS | SIGBUS | Misaligned memory access |
| EXC_CRASH | SIGABRT | Deliberate abort (assertion, fatalError, uncaught exception) |
| EXC_BREAKPOINT | SIGTRAP | Swift runtime trap (force unwrap nil, array bounds, etc.) |
| EXC_BAD_INSTRUCTION | SIGILL | Illegal instruction (corrupted code or deliberate trap) |

### Investigation Steps

1. Find the **faulting thread** and read its stack frames top-to-bottom
2. Look for **your code** in the frames (not system frameworks)
3. Check the **exception type** to understand the category of crash
4. Look at logs just before the crash time for context
5. If symbolication is incomplete, use \`atos\` to resolve addresses
`;

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
