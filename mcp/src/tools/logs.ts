import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { execSync } from "node:child_process";
import { homedir } from "node:os";
import { z } from "zod";
import { readStateFile } from "../config.js";
import { apiRequest } from "../http.js";

export function registerLogTools(server: McpServer): void {
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
          "quern start",  // Wrapper script in PATH (installed by setup)
          `${homedir()}/.local/bin/quern start`,  // Direct path to wrapper
          "quern-debug-server start",  // Legacy name
          `${homedir()}/.local/bin/quern-debug-server start`,  // Legacy direct path
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
        .enum(["syslog", "oslog", "crash", "build", "proxy", "app_drain", "simulator", "device", "server"])
        .optional()
        .describe("Filter by log source. Use 'server' to see Quern's own Python logs (startup, errors, tunnel resolution, adapter status) — useful for debugging the debug server itself."),
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
        .enum(["syslog", "oslog", "crash", "build", "proxy", "app_drain", "simulator", "device", "server"])
        .optional()
        .describe("Filter by log source. Use 'server' to see Quern's own Python logs (startup, errors, tunnel resolution, adapter status) — useful for debugging the debug server itself."),
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
      udid: z
        .string()
        .optional()
        .describe("Device UDID to pull fresh crashes from before returning results"),
    },
    async ({ limit, since, udid }) => {
      try {
        const data = await apiRequest("GET", "/api/v1/crashes/latest", {
          limit,
          since,
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
      source: z.string().describe("Source adapter to configure (e.g. 'simulator')"),
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
}
