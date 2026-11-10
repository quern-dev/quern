#!/usr/bin/env node

/**
 * Quern — MCP Server
 *
 * Thin wrapper that translates MCP tool calls into HTTP requests
 * to the Python log server running on localhost:9110.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { apiRequest, probeServer, serverFetch, sessionNotices } from "./http.js";
import { openHeldSession, startHeldSession, stopHeldSession, whenReopened } from "./held-session.js";
import { registerSessionTools } from "./tools/sessions.js";
import { deliverSessionNotices } from "./notices.js";
import { isClientKey, sharesDisk, uploadKnowledge } from "./remote-paths.js";
import { registerLogTools } from "./tools/logs.js";
import { registerProxyTools } from "./tools/proxy.js";
import { registerInterceptTools } from "./tools/intercept.js";
import { registerDeviceTools } from "./tools/device.js";
import { registerDeviceUITools } from "./tools/device-ui.js";
import { registerDevicePoolTools } from "./tools/device-pool.js";
import { registerSimulatorLogTools } from "./tools/simulator-log.js";
import { registerDeviceLogTools } from "./tools/device-log.js";
import { registerWdaTools } from "./tools/wda.js";
import { registerBuildTools } from "./tools/build.js";
import { registerAppStateTools } from "./tools/app-state.js";
import { registerOslogTools } from "./tools/oslog.js";
import { registerAppKnowledgeTools } from "./tools/app-knowledge.js";
import {
  autoloadKnowledge,
  reloadForNewSession,
  uploadKnowledgeForSession,
  detectProjectKnowledge,
  knowledgeInstructions,
  type KnowledgeOutcome,
} from "./project-knowledge.js";
import { registerLandmarkTools } from "./tools/landmarks.js";
import { registerSystemTools, whenSwitched } from "./tools/system.js";
import { registerRecordingTools } from "./tools/recordings.js";
import { registerProfileTools } from "./tools/profiles.js";
import { discoverServer } from "./config.js";
import { readOwnVersion } from "./version.js";
import { guidePages } from "./guide-pages.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

const MCP_VERSION = readOwnVersion();

// The project this session is in -- the client starts this process there.
const PROJECT_KNOWLEDGE = (() => {
  try {
    return detectProjectKnowledge();
  } catch {
    return null;
  }
})();

const BASE_INSTRUCTIONS = [
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
  "- See screen: get_screen_summary (quick) | get_ui_tree (full) | take_screenshot (visual) | take_annotated_screenshot (a11y overlay) | preview_device (live video, USB devices + booted simulators)",
  "- Identify screen by name (when a knowledge base is loaded): identify_screen | get_screen_summary?identify=true. Set up with load_landmarks, validate with validate_landmarks. The deterministic 'what screen am I on?' answer — use this instead of parsing labels yourself.",
  "- Interact: tap_element (preferred) | tap (coordinates, rare) | swipe | type_text (clear_text first if field has content)",
  "- Network: get_flow_summary → query_flows → get_flow_detail | wait_for_flow (block until match) | set_mock (synthetic responses) | set_intercept + release_flow (modify live traffic)",
  "- Logs: get_log_summary → query_logs | tail_logs (recent) | get_errors | get_latest_crash",
  "- Devices: resolve_device (find/boot/claim) | install_app | launch_app | terminate_app | uninstall_app | list_apps | grant_permission (sim only) | preview_device (live video) | stop_preview | preview_status",
  "- Device selection: use list_devices or resolve_device to discover devices. Prefer already-booted simulators or connected physical devices unless the user specifies otherwise.",
  "",
  "NETWORK CAPTURE:",
  "- Local capture (recommended for simulators): transparent, per-simulator flow tagging via simulator_udid. Check proxy_status local_capture field.",
  "- System proxy: configure_system_proxy to start, unconfigure_system_proxy when done. Always unconfigure when finished.",
  "- If no flows captured: verify certs with verify_proxy_setup, fix with install_proxy_cert",
  "",
  "PHYSICAL DEVICES: Call setup_wda once for first-time setup. After that, WDA auto-starts on first interaction. You have full UI control — use launch_app, tap_element, type_text, swipe, get_screen_summary to navigate iOS Settings, install certs, configure Wi-Fi, or perform any multi-step task on the device yourself. Use start_device_logging / stop_device_logging for logs. get_latest_crash with udid for crash reports.",
  "",
  "TROUBLESHOOTING: If tools fail with connection errors, call ensure_server to check/restart the server.",
  "",
  "For the agent guide, read the quern://guide resource: an overview with a tool map, and the topic guides it points to (quern://guide/ui, /network, /devices, /logs, /performance, /sharing). Each topic is a contents list of short pages, one per subject (quern://guide/<topic>/<page>), and the tool map names the exact page for each common job: read only the page your task needs.",
];

/** The agent guide's topic resources: quern://guide/<topic>, read from docs/agent-guide/<topic>.md. */
export const AGENT_GUIDE_TOPICS: ReadonlyArray<readonly [string, string]> = [
  ["ui", "Agent guide: reading the screen, taps and gestures, web views, scrolling to an element, landmarks"],
  ["network", "Agent guide: capturing traffic, local capture and the system proxy, certificates, mocks, intercepts"],
  ["devices", "Agent guide: choosing a device, physical iPhones and WDA, live preview, app state and plists, device settings, builds"],
  ["logs", "Agent guide: app logs and whether quern can see them, crashes, recordings, get_trace, screenshot timelines"],
  ["performance", "Agent guide: CPU and memory, hangs and janky frames, profiles, thread stacks, device conditions"],
  ["sharing", "Agent guide: shared servers, reservations, client keys, other Macs and use_remote"],
];

// Built in main(), after the knowledge base load: the instructions are fixed
// when the server is constructed, and they say how that load went rather than
// promising one that may not happen.
function buildServer(instructions: string): McpServer {
  const server = new McpServer(
    {
      name: "quern-debug-server",
      version: MCP_VERSION,
    },
    { instructions },
  );
  // Before any tool is registered: every tool result carries pending
  // held-session notices (#254).
  deliverSessionNotices(server);

  // ---------------------------------------------------------------------------
  // Tools
  // ---------------------------------------------------------------------------

  registerLogTools(server);
  registerProxyTools(server);
  registerRecordingTools(server);
  registerProfileTools(server);
  registerInterceptTools(server);
  registerDeviceTools(server);
  registerDeviceUITools(server);
  registerDevicePoolTools(server);
  registerSessionTools(server);
  registerSimulatorLogTools(server);
  registerDeviceLogTools(server);
  registerWdaTools(server);
  registerBuildTools(server);
  registerAppStateTools(server);
  registerOslogTools(server);
  registerAppKnowledgeTools(server);
  registerLandmarkTools(server);
  registerSystemTools(server);

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
        "Agent guide: principles, workflows, tool selection, and performance tips",
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

  // The guide's topics, each its own resource; quern://guide names them all.
  // A topic's page is its contents list, and each subject in it is a page of
  // its own (quern://guide/<topic>/<page>), so an agent reads only the section
  // its task needs -- measured, that halves the guide text an agent reads at
  // no cost in correctness. The contents page is the one list of a topic's
  // pages: each line naming one registers it, with that line as its
  // description. tests/test_agent_guide.py keeps the lists, the files and the
  // links in step.
  for (const [topic, description] of AGENT_GUIDE_TOPICS) {
    const uri = `quern://guide/${topic}`;
    server.resource(`guide-${topic}`, uri, { description, mimeType: "text/markdown" }, async () => ({
      contents: [{ uri, mimeType: "text/markdown", text: readResourceFile(`agent-guide/${topic}.md`) }],
    }));
    for (const [page, about] of guidePages(topic, readResourceFile(`agent-guide/${topic}.md`))) {
      const pageUri = `${uri}/${page}`;
      server.resource(`guide-${topic}-${page}`, pageUri, {
        description: `Agent guide, ${topic}: ${about}`, mimeType: "text/markdown",
      }, async () => ({
        contents: [{
          uri: pageUri, mimeType: "text/markdown",
          text: readResourceFile(`agent-guide/${topic}/${page}.md`),
        }],
      }));
    }
  }

  server.resource(
    "api-reference",
    "quern://api-reference",
    {
      description:
        "Complete REST API reference: every MCP tool and the endpoint behind it, plus the endpoints that have no tool (SSE streams, public probes). Read this when calling the HTTP API directly instead of through MCP.",
      mimeType: "text/markdown",
    },
    async () => ({
      contents: [
        {
          uri: "quern://api-reference",
          mimeType: "text/markdown",
          text: readResourceFile("api-reference.md"),
        },
      ],
    })
  );

  server.resource(
    "app-knowledge-guide",
    "quern://app-knowledge-guide",
    {
      description:
        "Guide for building an app knowledge base: how to conduct a guided tour, document screens, flows, deep links, and quirks",
      mimeType: "text/markdown",
    },
    async () => ({
      contents: [
        {
          uri: "quern://app-knowledge-guide",
          mimeType: "text/markdown",
          text: readResourceFile("app-knowledge-guide.md"),
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

  return server;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function checkVersionSkew(): Promise<void> {
  // Compare our package.json version against the Python server's
  // reported version. Mismatches happen when one side has been
  // updated but the other hasn't — the user gets weird behavior and
  // no signal about the cause. Best-effort: if /health is unreachable
  // or shape-mismatched, we just stay quiet (probeServer already
  // warns about unreachable). Cap timeout to 2s so a slow server
  // never blocks MCP startup.
  try {
    const server = discoverServer();
    const resp = await serverFetch(server, new URL("/health", server.url).toString(), {
      signal: AbortSignal.timeout(2000),
    });
    if (!resp.ok) return;
    const data = (await resp.json()) as { version?: string };
    const serverVersion = data?.version;
    if (
      serverVersion &&
      MCP_VERSION !== "unknown" &&
      serverVersion !== MCP_VERSION
    ) {
      console.error(
        `WARNING: Quern MCP version (${MCP_VERSION}) does not match server version (${serverVersion}). ` +
          `One side has been updated and the other hasn't. ` +
          `Run "quern update" to bring both in sync.`,
      );
    }
  } catch {
    // discoverServer can throw if state.json is missing; that's
    // probeServer's problem, not ours.
  }
}

/** Why the session-start load cannot send this project's path, or undefined
 * when it can: a server on another disk cannot read it, and the server-wide
 * sets are the owner's to load (#254, phase 3b). Either way the knowledge base
 * is uploaded and loaded for this session instead (#560). */
async function knowledgeSkip(): Promise<string | undefined> {
  try {
    const target = discoverServer();
    if (!(await sharesDisk(target))) {
      return "the quern server is another machine, which cannot read this project's " +
        "knowledge base, and its landmark sets are loaded on that machine by its owner";
    }
    if (isClientKey(target.apiKey)) {
      return "this MCP uses a client key, and loading landmark sets -- which every key " +
        "on the server shares -- is the owner's";
    }
  } catch {
    // Discovery failing is reported elsewhere; attempt the load as before.
  }
  return undefined;
}

async function main(): Promise<void> {
  const reachable = await probeServer();
  if (reachable) await checkVersionSkew();
  let knowledge: string[] = [];
  if (PROJECT_KNOWLEDGE) {
    // Best-effort, bounded, and skipped outright when the probe found no
    // server -- the client is waiting on this before it can connect.
    // A path the server cannot read, or a key that may not load the
    // server-wide sets: send the knowledge base instead, loaded for this
    // session alone (#560).
    const pathUnusable = reachable ? await knowledgeSkip() : undefined;
    const outcome: KnowledgeOutcome = pathUnusable
      ? await uploadKnowledgeForSession(
        PROJECT_KNOWLEDGE, reachable, openHeldSession,
        (dir, app) => uploadKnowledge(discoverServer(), dir, app), apiRequest)
      : await autoloadKnowledge(PROJECT_KNOWLEDGE, apiRequest, reachable);
    if (outcome.kind !== "none") console.error(`Knowledge base: ${JSON.stringify(outcome)}`);
    if (outcome.kind === "session_loaded") {
      // Loaded for this session, it is gone when the session is: a server
      // restart or a grace period that ran out. Loaded again for the new one,
      // and said if that fails, rather than left described as loaded (review).
      const pk = PROJECT_KNOWLEDGE;
      whenReopened(async () => {
        await reloadForNewSession(
          pk, (dir, app) => uploadKnowledge(discoverServer(), dir, app), apiRequest,
          (text) => sessionNotices.push(text));
      });
    }
    knowledge = knowledgeInstructions(PROJECT_KNOWLEDGE, outcome);
  }

  // After a use_remote switch: the same load against the new server, and the
  // same reload hook, which the switch cleared (held-session.ts).
  if (PROJECT_KNOWLEDGE) {
    const pk = PROJECT_KNOWLEDGE;
    whenSwitched(async () => {
      const unusable = await knowledgeSkip();
      const outcome: KnowledgeOutcome = unusable
        ? await uploadKnowledgeForSession(pk, true, async () => true,
          (dir, app) => uploadKnowledge(discoverServer(), dir, app), apiRequest)
        : await autoloadKnowledge(pk, apiRequest, true);
      if (outcome.kind === "session_loaded") {
        whenReopened(async () => {
          await reloadForNewSession(
            pk, (dir, app) => uploadKnowledge(discoverServer(), dir, app), apiRequest,
            (text) => sessionNotices.push(text));
        });
      }
      return outcome;
    });
  }

  const server = buildServer([...BASE_INSTRUCTIONS, ...knowledge].join("\n"));
  const transport = new StdioServerTransport();
  await server.connect(transport);

  // The held session (#254): opened in the background, so a server that is
  // down never delays the client. Released cleanly when the client goes away
  // -- and that release is also what lets this process exit, because an open
  // WebSocket keeps Node's event loop alive.
  startHeldSession();
  const finish = () => {
    stopHeldSession();
    setTimeout(() => process.exit(0), 300).unref();
  };
  // The SDK's own close hook: the transport's `onclose` belongs to the SDK,
  // which installs its handler there on connect.
  server.server.onclose = finish;
  process.stdin.on("end", finish);
  process.stdin.on("close", finish);
  process.on("SIGTERM", finish);
  process.on("SIGINT", finish);

  console.error(`Quern Debug MCP Server v${MCP_VERSION} running on stdio`);
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
