import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { execSync } from "node:child_process";
import { homedir } from "node:os";
import { z } from "zod";
import { readStateFile } from "../config.js";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerLogTools(server: McpServer): void {
  server.registerTool("ensure_server", {
    description: `Ensure Quern is running. Reads state.json, health checks, and starts the server if needed. This is the recommended first tool call for any agent session. Returns connection info including server URL, proxy port, and API key.`,
    inputSchema: strictParams({}),
  }, async () => {
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
                // Best-effort: surface the cached update-check result so
                // Claude can mention "v0.13.5 is available" inline. The
                // endpoint never blocks; if it errors we just omit the
                // field. update_available=false is intentionally elided
                // to keep the response quiet on the happy path.
                let updateAvailable: unknown = undefined;
                try {
                  const upd = (await apiRequest(
                    "GET",
                    "/api/v1/system/update-status",
                    undefined,
                    undefined,
                    2000,
                  )) as { update_available?: boolean } | null;
                  if (upd && upd.update_available) {
                    updateAvailable = upd;
                  }
                } catch {
                  // Older server or transient failure — skip silently.
                }
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
                          ...(updateAvailable
                            ? { update_available: updateAvailable }
                            : {}),
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
                  "Error: Failed to start Quern.",
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

  server.registerTool("tail_logs", {
    description: `Show recent log entries (most recent first). Use this for quick "what just happened?" queries. Defaults to the 50 most recent entries.`,
    inputSchema: strictParams({
      count: z
        .coerce.number()
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
        .enum(["syslog", "oslog", "crash", "build", "proxy", "app_drain", "simulator", "device", "logcat", "plist_watcher", "server"])
        .optional()
        .describe("Filter by log source. Use 'server' to see Quern's own Python logs (startup, errors, tunnel resolution, adapter status) — useful for debugging the debug server itself."),
    }),
  }, async ({ count, level, process, source }) => {
      try {
        const data = (await apiRequest("GET", "/api/v1/logs/query", {
          limit: count,
          level,
          process,
          source,
          tail: true,
        })) as { entries: unknown[] };

        const entries = data.entries || [];

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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs Quern running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool("query_logs", {
    description: `Full-featured log search with time ranges and text search. Use this for investigating specific issues — filter by time, process, level, or search text.`,
    inputSchema: strictParams({
      since: z
        .string()
        .optional()
        .describe("Start time (ISO 8601, e.g. 2026-02-08T10:00:00Z). No offset means UTC."),
      until: z
        .string()
        .optional()
        .describe("End time (ISO 8601). No offset means UTC."),
      level: z
        .enum(["debug", "info", "notice", "warning", "error", "fault"])
        .optional()
        .describe("Minimum log level"),
      process: z.string().optional().describe("Filter by process name"),
      source: z
        .enum(["syslog", "oslog", "crash", "build", "proxy", "app_drain", "simulator", "device", "logcat", "plist_watcher", "server"])
        .optional()
        .describe("Filter by log source. Use 'server' to see Quern's own Python logs (startup, errors, tunnel resolution, adapter status) — useful for debugging the debug server itself."),
      search: z
        .string()
        .optional()
        .describe("Text search within log messages"),
      limit: z
        .coerce.number()
        .min(1)
        .max(1000)
        .default(100)
        .describe("Max entries to return"),
      offset: z.coerce.number().min(0).default(0).describe("Pagination offset"),
    }),
  }, async ({ since, until, level, process, source, search, limit, offset }) => {
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs Quern running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool("get_log_summary", {
    description: `Get an AI-optimized summary of recent log activity. Returns error counts, top issues, and a natural language summary. Supports cursor-based polling for efficient delta updates.`,
    inputSchema: strictParams({
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
    }),
  }, async ({ window, process, since_cursor }) => {
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs Quern running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool("get_errors", {
    description: `Get error-level log entries and crash reports. Useful for quickly finding what's going wrong.`,
    inputSchema: strictParams({
      since: z
        .string()
        .optional()
        .describe("Only errors after this time (ISO 8601). No offset means UTC."),
      limit: z
        .coerce.number()
        .min(1)
        .max(1000)
        .default(50)
        .describe("Max entries to return"),
      include_crashes: z
        .coerce.boolean()
        .default(true)
        .describe("Include crash reports in results"),
    }),
  }, async ({ since, limit, include_crashes }) => {
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs Quern running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool("get_build_result", {
    description: `Get the most recent parsed xcodebuild result, including errors, warnings, and test results.`,
    inputSchema: strictParams({}),
  }, async () => {
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

        // Return concise summary on success, full JSON on failure
        const resp = data as Record<string, unknown>;
        if (resp.succeeded && resp.summary) {
          return {
            content: [
              { type: "text" as const, text: resp.summary as string },
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs Quern running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool("parse_build_output", {
    description: `Parse an xcodebuild log file into structured errors, warnings, and test results. Run xcodebuild however you want (via Bash), pipe output to a file, then hand the file to this tool for structured parsing.`,
    inputSchema: strictParams({
      file_path: z
        .string()
        .describe("Absolute path to the xcodebuild log file (e.g. /tmp/build.log)"),
      fuzzy_groups: z
        .coerce.boolean()
        .optional()
        .describe("Use fuzzy word-level template grouping to collapse similar warnings (e.g. conformance warnings differing only by type name). Default: true. Set to false for exact match grouping."),
    }),
  }, async ({ file_path, fuzzy_groups }) => {
      try {
        const body: Record<string, unknown> = { file_path };
        if (fuzzy_groups !== undefined) {
          body.fuzzy_groups = fuzzy_groups;
        }
        const data = await apiRequest(
          "POST",
          "/api/v1/builds/parse-file",
          undefined,
          body
        );

        // Return concise summary on success, full JSON on failure
        const resp = data as Record<string, unknown>;
        if (resp.succeeded && resp.summary) {
          return {
            content: [
              { type: "text" as const, text: resp.summary as string },
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs Quern running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool("get_latest_crash", {
    description: `Get recent crash reports with parsed exception types, signals, and stack frames.`,
    inputSchema: strictParams({
      limit: z
        .coerce.number()
        .min(1)
        .max(100)
        .default(10)
        .describe("Max crash reports to return"),
      since: z
        .string()
        .optional()
        .describe("Only crashes after this time (ISO 8601). No offset means UTC."),
      udid: z
        .string()
        .optional()
        .describe("Device UDID to pull fresh crashes from before returning results"),
    }),
  }, async ({ limit, since, udid }) => {
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs Quern running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool("set_log_filter", {
    description: `Configure the ingestion filter to drop noisy log entries before they reach the ring buffer. Supports presets ("device-quiet", "simulator-quiet") and per-field overrides. Filters can be scoped globally, per-source, or per-device (most specific wins). Use get_log_filter to see current state.

When a process include filter is set, running log adapters are automatically restarted with subprocess-level filtering (e.g. pymobiledevice3 -pn flag or simctl --predicate). This cuts noise at the source instead of just filtering in Python. The response includes "adapter_restarted": true when this happens.

For app-only filtering (zero noise), combine process + subsystems include. First find your app's subsystem by calling tail_logs with the process filter, then lock it down:
  set_log_filter(source: "device", process: "MyApp", subsystems: ["MyApp.debug.dylib"])
This eliminates all framework noise (UIKitCore, CFNetwork, Security) and shows only your code's os_log output.`,
    inputSchema: strictParams({
      source: z
        .enum(["syslog", "oslog", "crash", "build", "proxy", "app_drain", "simulator", "device", "logcat", "plist_watcher", "server"])
        .optional()
        .describe("Scope filter to this source adapter"),
      device_id: z
        .string()
        .optional()
        .describe("Scope filter to this device UDID (most specific, overrides source/global)"),
      process: z
        .string()
        .optional()
        .describe("Include only entries from this process (exact match)"),
      processes: z
        .array(z.string())
        .optional()
        .describe("Include only entries from these processes"),
      subsystems: z
        .array(z.string())
        .optional()
        .describe("Include only entries from these subsystems"),
      exclude_processes: z
        .array(z.string())
        .optional()
        .describe("Drop entries from these processes"),
      exclude_subsystems: z
        .array(z.string())
        .optional()
        .describe("Drop entries from these subsystems"),
      exclude_messages: z
        .array(z.string())
        .optional()
        .describe("Drop entries whose message contains any of these substrings (case-insensitive)"),
      min_level: z
        .enum(["debug", "info", "notice", "warning", "error", "fault"])
        .optional()
        .describe("Drop entries below this severity level"),
      preset: z
        .enum(["device-quiet", "simulator-quiet"])
        .optional()
        .describe("Load a named preset as base config (can be combined with other fields as overrides). device-quiet excludes common system daemons (bluetoothd, wifid, kernel, symptomsd, remotepairingdeviced, signpost_reporter) and noisy subsystems (CoreBrightness, CFNetwork)."),
    }),
  }, async ({ source, device_id, process, processes, subsystems, exclude_processes, exclude_subsystems, exclude_messages, min_level, preset }) => {
      try {
        const body: Record<string, unknown> = {};
        if (source !== undefined) body.source = source;
        if (device_id !== undefined) body.device_id = device_id;
        if (process !== undefined) body.process = process;
        if (processes !== undefined) body.processes = processes;
        if (subsystems !== undefined) body.subsystems = subsystems;
        if (exclude_processes !== undefined) body.exclude_processes = exclude_processes;
        if (exclude_subsystems !== undefined) body.exclude_subsystems = exclude_subsystems;
        if (exclude_messages !== undefined) body.exclude_messages = exclude_messages;
        if (min_level !== undefined) body.min_level = min_level;
        if (preset !== undefined) body.preset = preset;

        const data = await apiRequest("POST", "/api/v1/logs/filter", undefined, body);

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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs Quern running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool("get_log_filter", {
    description: `Show the current ingestion filter configuration at all scopes (global, per-source, per-device).`,
    inputSchema: strictParams({}),
  }, async () => {
      try {
        const data = await apiRequest("GET", "/api/v1/logs/filter");

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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs Quern running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool("get_trace", {
    description: `One timeline: what quern did, the network flows each action caused, and the app log lines that arrived while it ran.

Use this when something went wrong and you want the sequence rather than three separate queries — "what happened when I tapped Submit" is one call instead of correlating query_logs and query_flows by eye.

Each action carries its resolved device, outcome and duration. Flows and log lines are attributed to the action whose interval contains them, on the same device.

Flows are attributed to an action if they happen during it, or within a few seconds after it returns — most actions hand work to the device and return before the request goes out. Anything attributed that way is marked in \`caveats\`, because timing is not observed causation.

Every flow and log line carries \`identified_by\`, saying how its device was established: \`process\` (exact — resolved from the client pid under local capture), \`client_ip\` (an address recorded at proxy setup, which DHCP can reassign), \`client_ip_expired\` (recorded longer ago than quern will vouch for), \`adapter\` (a log line the capturing adapter named), or \`unidentified\` (only time connects it to the action). Weight an attribution by that field — it is stated on every item so you never have to infer confidence from a missing one.

Read \`caveats\` and \`overlaps\` before trusting an attribution. Attribution is by device and time, because the proxy is a separate process and nothing quern controls travels with the app's requests. Two actions overlapping on one device cannot be told apart, and that is reported rather than guessed. Simulators under local capture (see set_local_capture) attribute most precisely, because flows carry a UDID resolved from the client process.

With several agents on one server, pass \`udid\` to get only your own device's actions.`,
    inputSchema: strictParams({
      since: z
        .string()
        .optional()
        .describe("Start time (ISO 8601). No offset means UTC. Defaults to the last 5 minutes."),
      udid: z
        .string()
        .optional()
        .describe("Only actions against this device"),
      limit: z
        .number()
        .int()
        .min(1)
        .max(1000)
        .optional()
        .describe("Maximum actions to return (1-1000, default 100)"),
    }),
  }, async ({ since, udid, limit }) => {
      try {
        const params: Record<string, string | number | boolean | undefined> = {};
        if (since) params.since = since;
        if (udid) params.udid = udid;
        if (limit) params.limit = limit;

        const data = await apiRequest("GET", "/api/v1/trace", params);

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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs Quern running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
  });

  server.registerTool("list_log_sources", {
    description: `List all active log source adapters and their current status (streaming, watching, stopped, error).`,
    inputSchema: strictParams({}),
  }, async () => {
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs Quern running? Start it with: quern-debug-server`,
            },
          ],
          isError: true,
        };
      }
    }
  );
}
