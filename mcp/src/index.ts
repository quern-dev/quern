#!/usr/bin/env node

/**
 * iOS Debug Server — MCP Server
 *
 * Thin wrapper that translates MCP tool calls into HTTP requests
 * to the Python log server running on localhost:9100.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { z } from "zod";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const SERVER_URL =
  process.env.IOS_DEBUG_SERVER_URL || "http://127.0.0.1:9100";

function loadApiKey(): string {
  try {
    const keyPath = join(homedir(), ".ios-debug-server", "api-key");
    return readFileSync(keyPath, "utf-8").trim();
  } catch {
    console.error(
      "WARNING: Could not read API key from ~/.ios-debug-server/api-key"
    );
    return "";
  }
}

const API_KEY = loadApiKey();

// ---------------------------------------------------------------------------
// HTTP helper
// ---------------------------------------------------------------------------

async function apiRequest(
  method: "GET" | "POST",
  path: string,
  params?: Record<string, string | number | boolean | undefined>,
  body?: unknown
): Promise<unknown> {
  const url = new URL(path, SERVER_URL);

  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null) {
        url.searchParams.set(k, String(v));
      }
    }
  }

  const headers: Record<string, string> = {
    Authorization: `Bearer ${API_KEY}`,
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
  try {
    await fetch(new URL("/health", SERVER_URL).toString(), {
      signal: AbortSignal.timeout(3000),
    });
    console.error(`Connected to iOS Debug Server at ${SERVER_URL}`);
  } catch {
    console.error(
      `ERROR: Cannot reach iOS Debug Server at ${SERVER_URL} — is it running?`
    );
  }
}

// ---------------------------------------------------------------------------
// MCP Server
// ---------------------------------------------------------------------------

const server = new McpServer({
  name: "ios-debug-server",
  version: "0.1.0",
});

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the iOS Debug Server running? Start it with: ios-debug-server`,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the iOS Debug Server running? Start it with: ios-debug-server`,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the iOS Debug Server running? Start it with: ios-debug-server`,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the iOS Debug Server running? Start it with: ios-debug-server`,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the iOS Debug Server running? Start it with: ios-debug-server`,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the iOS Debug Server running? Start it with: ios-debug-server`,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the iOS Debug Server running? Start it with: ios-debug-server`,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the iOS Debug Server running? Start it with: ios-debug-server`,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the iOS Debug Server running? Start it with: ios-debug-server`,
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}\n\nIs the iOS Debug Server running? Start it with: ios-debug-server`,
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

const GUIDE_CONTENT = `# iOS Debug Server — Tool Selection Guide

## Quick Reference

| I want to…                        | Use this tool        |
|-----------------------------------|----------------------|
| See what just happened            | \`tail_logs\`          |
| Search for specific errors        | \`query_logs\`         |
| Get an AI-friendly status update  | \`get_log_summary\`    |
| See only errors and crashes       | \`get_errors\`         |
| Check build results               | \`get_build_result\`   |
| Investigate a crash               | \`get_latest_crash\`   |
| Change what's being captured      | \`set_log_filter\`     |
| See captured HTTP traffic         | \`query_flows\`        |
| Inspect a specific HTTP flow      | \`get_flow_detail\`    |
| Check which sources are active    | \`list_log_sources\`   |

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

### 5. Debugging a Specific Issue

1. \`query_logs\` with \`search\` to find relevant messages
2. \`query_logs\` with \`process\` and time range to narrow down
3. \`get_log_summary\` for the big picture

## Tips

- **tail_logs vs query_logs**: Use \`tail_logs\` for "show me recent stuff" (defaults to 50, newest first). Use \`query_logs\` for searching with filters and time ranges.
- **Level filtering**: \`level: "error"\` returns ERROR and FAULT entries.
- **Sources**: \`syslog\` = device system log, \`oslog\` = macOS unified log, \`crash\` = crash reports, \`build\` = xcodebuild output, \`proxy\` = network traffic.
`;

const TROUBLESHOOTING_CONTENT = `# iOS Debug Server — Troubleshooting Guide

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

  console.error("iOS Debug MCP Server running on stdio");
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
