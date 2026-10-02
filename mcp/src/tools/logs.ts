import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { execSync } from "node:child_process";
import { homedir } from "node:os";
import { z } from "zod";
import { readStateFile } from "../config.js";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";
import { type LogQueryAnswer, tailLogsResult } from "./log-responses.js";

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
    description: `Show recent log entries (most recent first). Use this for quick "what just happened?" queries. Defaults to the 50 most recent entries. If the result has \`truncated: true\`, entries in the window were evicted before you asked, so an empty or short answer does NOT mean nothing happened — \`complete_after\` says from when the answer is whole.`,
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
        })) as LogQueryAnswer;

        // Shaped by a function the test suite runs; see log-responses.ts.
        return {
          content: [
            {
              type: "text" as const,
              text: JSON.stringify(tailLogsResult(data), null, 2),
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
    description: `Full-featured log search with time ranges and text search. Use this for investigating specific issues — filter by time, process, level, or search text. If the result has \`truncated: true\`, entries in the window were evicted before you asked, so an empty or short answer does NOT mean nothing happened — \`complete_after\` says from when the answer is whole.`,
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
    description: `Get an AI-optimized summary of recent log activity. Returns error counts, top issues, and a natural language summary. Supports cursor-based polling for efficient delta updates. If \`truncated\` is true, entries in the window were evicted and the counts may be low. The cursor follows arrival order: a delta returns everything that arrived since the last summary, including entries stamped earlier (a device clock ahead of the host, a crash report written after the crash). If \`cursor_reset\` is true, the cursor could not be honoured (the server restarted, the cursor is ahead of anything the server has numbered, or it is not a cursor) and the result covers the window instead.`,
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
    description: `Get error-level log entries and crash reports. Useful for quickly finding what's going wrong. \`truncated: true\` means error-level entries from the window were evicted before you asked, so an empty list is not proof there were no errors.`,
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
    description: `Get the most recent parsed xcodebuild result, including errors, warnings, and test results. A failed build always names a cause: a location-less error (signing, provisioning), or the steps xcodebuild says failed.`,
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
    description: `Parse an xcodebuild log file into structured errors, warnings, and test results. Run xcodebuild however you want (via Bash), pipe output to a file, then hand the file to this tool for structured parsing. Errors include ones with no source location (signing, provisioning), and a build that failed with nothing compiled wrong reports the steps xcodebuild says failed -- a package plug-in xcodebuild would not run unapproved among them, with how to get past it.`,
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
    description: `Get recent crash reports with parsed exception types, signals, and stack frames. Each report's \`app_frame\` is where in the app's own code it happened -- from an uncaught exception's backtrace when there is one, else the crashing thread, skipping a crash reporter's signal handler and the app's entry point -- with source file and line when the report has them (simulator Debug builds do; a phone names the function for a Debug build, an offset for a stripped one). \`reason\` is the report's own explanation (an uncaught exception's reason; Android's abort message or root cause); a Swift fatalError's message is in the app's log, not the report. \`killed_by\` names another process whose signal ended the app (a kill from a shell or devicectl): then there is no crash site, only where it was waiting. A watchdog keeps its frames -- they are where it hung -- and its explanation goes in \`reason\`. \`top_frames\` is the first few frames as text. A device report's app frames are symbolicated against the build that crashed: matched by UUID to one of quern's build records (a build_and_install to a device keeps its dSYMs), or to a dSYM Spotlight indexed in Xcode's DerivedData or archives, and resolved with atos -- so a phone's crash gets its source file and line, which the phone itself does not record. \`symbols\` says, per image, where they came from, or that nothing on this Mac matched that UUID (never a guess). On Android, a minified build's Java frames are retraced against its R8 mapping.txt (the whole trace, inlined frames listed one per function, and the app frame chosen again from the real names) and native frames resolved by BuildId with the NDK's llvm-symbolizer, once the build has been recorded with record_android_build; without an r8-map-id in the trace, the mapping is matched by package and version, which \`symbols\` says. Pass \`symbolicate=false\` to skip it. Pass \`detail\` for every frame and the images' UUIDs and load addresses. Pass \`udid\` to fetch a device's crashes first: an iPhone over USB (pymobiledevice3), or an Android device or emulator (its DropBox: Java crashes, native crashes, and ANRs -- \`kind\` says which). The response's \`pull\` says whether that fetch happened: 'pulled', 'skipped' (with the reason, e.g. an iPhone not on USB) or 'failed' (with the error). An iPhone pull reaches back \`days\` (default 3) and says what it left on the phone in \`pull.older_on_device\` / \`pull.note\`; pass a larger \`days\` to reach further. Only 'pulled' means the list reflects the device (for an iPhone, within the window); otherwise an empty list is not proof of no crashes (a 'failed' Android pull may still add the reports it could read; a simulator is 'skipped' because its reports are read continuously on the Mac). On Android, \`pull.open_dialogs\` names processes showing a crash dialog, or that Android is treating as not responding ('anr', which starts before its report exists): while a crash dialog is open, Android silently drops that process's further crashes, so dismiss it or force-stop the app before reproducing. With \`udid\`, the list is that device's crashes, plus reports quern cannot place on a device; a simulator's crashes carry its UDID, read from the app's path or, for its system apps, the report's coalition. A crash of one of the Mac's own processes (\`mac_process\`) is listed only without \`udid\`.`,
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
      days: z
        .coerce.number()
        .int()
        .min(1)
        .max(3650)
        .optional()
        .describe("iPhone: how far back the pull reaches, in days (default 3). Older reports stay on the phone and are counted in pull.older_on_device."),
      // A real boolean or the two string spellings, as start_proxy's system_proxy.
      detail: z
        .union([z.boolean(), z.enum(["true", "false"]).transform((v) => v === "true")])
        .optional()
        .describe("Include each report's full frames and images (UUIDs, load addresses) -- what symbolicating a crash needs. Off by default: several kilobytes per crash."),
      include_raw: z
        .union([z.boolean(), z.enum(["true", "false"]).transform((v) => v === "true")])
        .optional()
        .describe("Include each report's raw_text, the start of the report as written (about a thousand tokens per crash). Off by default."),
      symbolicate: z
        .union([z.boolean(), z.enum(["true", "false"]).transform((v) => v === "true")])
        .optional()
        .describe("Resolve a device report's app frames to function, file and line against the build that crashed (on by default). Most of a second per image the first time; kept after, until the server restarts. An image whose symbols were not found is looked up again on the next read, so a build made later is picked up."),
    }),
  }, async ({ limit, since, udid, days, detail, include_raw, symbolicate }) => {
      try {
        const data = await apiRequest("GET", "/api/v1/crashes/latest", {
          limit,
          since,
          udid,
          days,
          detail,
          include_raw,
          symbolicate,
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

  server.registerTool("clear_crashes", {
    description: `Delete the crash reports quern has stored on this Mac: one device's (pass \`udid\`) or all of them. Deletes only the copies quern's own pulls wrote, and drops reports from get_latest_crash's list; never deletes anything else in the crash directory, and never ~/Library/Logs/DiagnosticReports, which belongs to the Mac. It does not clear the device: a later get_latest_crash lists again whatever the device still holds within its window (without logging it as a new crash). To remove an iPhone's own reports, use clear_device_crashes. An unknown \`udid\` is an error, not a success.`,
    inputSchema: strictParams({
      udid: z
        .string()
        .min(1)
        .optional()
        .describe("Clear only this device's stored reports; omit to clear all"),
    }),
  }, async ({ udid }) => {
      try {
        const data = await apiRequest("DELETE", "/api/v1/crashes", { udid });

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

  server.registerTool("clear_device_crashes", {
    description: `PERMANENTLY delete the crash reports on an iPhone (over USB): every .ips/.crash report at the top of its crash directory, recent and old alike. They are gone for Xcode, Finder and anything else that reads them, not only for quern — only call this when the user has asked for the device's crash reports to be removed, for example after get_latest_crash reported a large backlog in pull.older_on_device. DiagnosticLogs (sysdiagnose archives) and other files there are left alone. Returns how many were removed, how many remain, and any the phone would not delete. quern's copies on the Mac stay, subject to the 30-day retention (clear_crashes removes them). Refused for a phone matched to USB by name rather than its hardware UDID, for Android (an unrooted device's crash store can only be read), and for a simulator (its reports are files on the Mac).`,
    inputSchema: strictParams({
      udid: z
        .string()
        .min(1)
        .describe("The iPhone whose crash reports to delete"),
    }),
  }, async ({ udid }) => {
      try {
        const data = await apiRequest("POST", "/api/v1/crashes/device/clear", undefined, { udid });

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
      quiet_subsystems: z
        .array(z.string())
        .optional()
        .describe("Subsystem prefixes (e.g. 'com.apple.') whose entries are kept only at quiet_below and above: their chatter dropped, their errors kept. Unlike exclude_subsystems, which drops every level."),
      quiet_below: z
        .enum(["debug", "info", "notice", "warning", "error", "fault"])
        .optional()
        .describe("The level a quiet_subsystems entry must reach to be kept (default error)"),
      preset: z
        .enum(["device-quiet", "simulator-quiet"])
        .optional()
        .describe("Load a named preset as base config (can be combined with other fields as overrides). device-quiet excludes common system daemons (bluetoothd, wifid, kernel, symptomsd, remotepairingdeviced, signpost_reporter) and noisy subsystems (CoreBrightness, CFNetwork). simulator-quiet drops HangTracer and com.apple.CoreFoundation, and every com.apple.* entry below error -- Apple's frameworks' chatter inside the app, with their errors kept."),
    }),
  }, async ({ source, device_id, process, processes, subsystems, exclude_processes, exclude_subsystems, exclude_messages, min_level, quiet_subsystems, quiet_below, preset }) => {
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
        if (quiet_subsystems !== undefined) body.quiet_subsystems = quiet_subsystems;
        if (quiet_below !== undefined) body.quiet_below = quiet_below;
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

Every flow and log line carries \`identified_by\`, saying how its device was established: \`process\` (exact — resolved from the client pid: a \`launchd_sim\` ancestor for an iOS simulator, or the process owning the socket for an Android emulator), \`client_ip\` (an address recorded at proxy setup, which DHCP can reassign), \`client_ip_expired\` (recorded longer ago than quern will vouch for), \`adapter\` (a log line the capturing adapter named), or \`unidentified\` (only time connects it to the action). Weight an attribution by that field — it is stated on every item so you never have to infer confidence from a missing one.

Read \`caveats\` and \`overlaps\` before trusting an attribution. Attribution is by device and time, because the proxy is a separate process and nothing quern controls travels with the app's requests. Two actions overlapping on one device cannot be told apart, and that is reported rather than guessed. Simulators under local capture (see set_local_capture) attribute most precisely, because flows carry a UDID resolved from the client process.

With several agents on one server, pass \`udid\` to get only your own device's actions.

Pass \`recording\` (an id from start_recording, or the directory it wrote) to build the same trace from a recording instead of the live buffers, over any window of it (\`since\`, \`until\`) -- for a run longer than the buffers hold. Its \`recording.holes\` lists spans the recording says it does not cover, and the *_truncated fields are set when one overlaps the window. A recording made with video gives each action and flow \`video: {path, offset_s}\` -- the movie and the offset to seek to -- or null when no movie of its quern run covers it, and lists the movies in \`recording.video\`, each with an \`error\` if it was lost.`,
    inputSchema: strictParams({
      since: z
        .string()
        .optional()
        .describe("Start time (ISO 8601). No offset means UTC. Defaults to the last 5 minutes; with recording, to the recording's start."),
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
      recording: z
        .string()
        .optional()
        .describe("Read from this recording (its id, or the directory it wrote) instead of the live buffers"),
      until: z
        .string()
        .optional()
        .describe("End time (ISO 8601). With recording: defaults to its end"),
    }),
  }, async ({ since, udid, limit, recording, until }) => {
      try {
        const params: Record<string, string | number | boolean | undefined> = {};
        if (since) params.since = since;
        if (udid) params.udid = udid;
        if (limit) params.limit = limit;
        if (recording) params.recording = recording;
        if (until) params.until = until;

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
    description: `List all active log source adapters and their current status (streaming, watching, stopped, error). \`buffers\` shows what each log buffer holds and what it lost: capacity, evictions by source, and the oldest entry still queryable. A source's \`entries_captured\` is intake, not retention — compare it with \`buffers.logs.evicted\` to see whether capture is outrunning the buffer; if it is, filter at the source with \`process\` or \`subsystem\`.`,
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
