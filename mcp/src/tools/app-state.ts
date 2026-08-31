import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerAppStateTools(server: McpServer): void {
  // ---------------------------------------------------------------------------
  // Checkpoint tools
  // ---------------------------------------------------------------------------

  server.registerTool("save_app_state", {
    description: `Save a named checkpoint of a simulator app's state. Captures the data container and all app group containers. Terminates the app before copying. Simulator only.

Use this to snapshot known-good states (e.g. "logged_in", "staging_configured") so you can restore them later without reinstalling.

IMPORTANT for logged-in states: auth tokens live in the simulator keychain, which is NOT part of any app container. Without include_keychain a checkpoint always restores to a logged-out app, however it was captured. Pass include_keychain: true to capture it — this requires the device to be shut down first (xcrun simctl shutdown <udid>), because the keychain is a WAL-mode SQLite database held open by securityd.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier (e.g. com.example.MyApp)"),
      label: z.string().describe('Short name for this checkpoint (e.g. "logged_in", "fresh_install")'),
      description: z.string().optional().describe("Human-readable description of this state"),
      include_keychain: z.boolean().optional().describe("Also capture the simulator keychain, so the checkpoint can restore a logged-in session. Requires the device to be shut down."),
      udid: z.string().optional().describe("Simulator UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, label, description, include_keychain, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id, label };
      if (description) body.description = description;
      if (include_keychain !== undefined) body.include_keychain = include_keychain;
      if (udid) body.udid = udid;
      const data = await apiRequest("POST", "/api/v1/device/app/state/save", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("restore_app_state", {
    description: `Restore a named app state checkpoint. Terminates the app, wipes live container contents, and copies the checkpoint back using re-resolved live paths (handles UUID rotation on reinstall). Simulator only.

If the checkpoint carries a keychain (saved with include_keychain), it is restored too and the app comes up logged in — this requires the device to be shut down, and the call fails before touching anything if it is not. A checkpoint saved without a keychain always restores to a logged-out app; the response's meta.keychain says which happened.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      label: z.string().describe("Name of the checkpoint to restore"),
      include_keychain: z.boolean().optional().describe("Defaults to restoring the keychain when the checkpoint has one. Set false to skip it and start logged out."),
      udid: z.string().optional().describe("Simulator UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, label, include_keychain, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id, label };
      if (include_keychain !== undefined) body.include_keychain = include_keychain;
      if (udid) body.udid = udid;
      const data = await apiRequest("POST", "/api/v1/device/app/state/restore", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("list_app_states", {
    description: `List all saved state checkpoints for an app. Returns metadata including label, description, and capture timestamp.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
    }),
  }, async ({ bundle_id }) => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/app/state/list", { bundle_id });
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("delete_app_state", {
    description: `Delete a named app state checkpoint.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      label: z.string().describe("Name of the checkpoint to delete"),
    }),
  }, async ({ bundle_id, label }) => {
    try {
      const data = await apiRequest(
        "DELETE",
        `/api/v1/device/app/state/${encodeURIComponent(label)}`,
        { bundle_id }
      );
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  // ---------------------------------------------------------------------------
  // Plist tools
  // ---------------------------------------------------------------------------

  server.registerTool("read_app_plist", {
    description: `Read a plist file (or single key) from a simulator app's container. Useful for inspecting feature flags, cached tokens, or any preference stored in a plist.

Container can be "data" (main data container) or a group ID like "group.com.example".
plist_path is relative to the container root (e.g. "Library/Preferences/com.example.plist").
If key is omitted, returns the entire plist as JSON.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      container: z.string().describe('"data" or a group ID (e.g. "group.com.example")'),
      plist_path: z.string().describe("Relative path to the plist within the container"),
      key: z.string().optional().describe("Specific key to read (omit to return entire plist)"),
      udid: z.string().optional().describe("Simulator UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, container, plist_path, key, udid }) => {
    try {
      const params: Record<string, string | undefined> = { bundle_id, container, plist_path };
      if (key) params.key = key;
      if (udid) params.udid = udid;
      const data = await apiRequest("GET", "/api/v1/device/app/state/plist", params);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("set_app_plist_value", {
    description: `Set a key in a plist file inside a simulator app's container. More surgical than a full state restore — flip a single feature flag without touching anything else. Simulator only.

Type inference: boolean values set -bool, integers set -integer, floats set -float, everything else -string.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      container: z.string().describe('"data" or a group ID (e.g. "group.com.example")'),
      plist_path: z.string().describe("Relative path to the plist within the container"),
      key: z.string().describe("Plist key to set"),
      value: z.union([z.string(), z.coerce.number(), z.coerce.boolean()]).describe("Value to set (type is inferred)"),
      udid: z.string().optional().describe("Simulator UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, container, plist_path, key, value, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id, container, plist_path, key, value };
      if (udid) body.udid = udid;
      const data = await apiRequest("POST", "/api/v1/device/app/state/plist", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("set_app_plist_values", {
    description: `Set multiple keys in a plist file in one call. More efficient than calling set_app_plist_value repeatedly — set all coaching flags, feature flags, or preferences at once. Simulator only.

Type inference per value: boolean → -bool, integer → -integer, float → -float, everything else → -string.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      container: z.string().describe('"data" or a group ID (e.g. "group.com.example")'),
      plist_path: z.string().describe("Relative path to the plist within the container"),
      values: z.record(z.string(), z.union([z.string(), z.number(), z.boolean()])).describe("Object mapping plist keys to values (e.g. {\"flag1\": true, \"flag2\": false, \"count\": 42})"),
      udid: z.string().optional().describe("Simulator UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, container, plist_path, values, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id, container, plist_path, values };
      if (udid) body.udid = udid;
      const data = await apiRequest("POST", "/api/v1/device/app/state/plist/batch", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("diff_app_plist", {
    description: `Compare a live plist against a saved checkpoint to see what changed. Returns added, removed, and changed keys. Useful for discovering new flags after app updates or verifying that a state restore worked correctly. Simulator only.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      container: z.string().describe('"data" or a group ID (e.g. "group.com.example")'),
      plist_path: z.string().describe("Relative path to the plist within the container"),
      checkpoint_label: z.string().describe("Name of the saved checkpoint to compare against"),
      udid: z.string().optional().describe("Simulator UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, container, plist_path, checkpoint_label, udid }) => {
    try {
      const params: Record<string, string | undefined> = {
        bundle_id, container, plist_path, checkpoint_label,
      };
      if (udid) params.udid = udid;
      const data = await apiRequest("GET", "/api/v1/device/app/state/plist/diff", params);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("delete_app_plist_key", {
    description: `Remove a key from a plist file inside a simulator app's container. Useful for simulating missing/unset state (e.g. remove a flag to trigger first-launch flow). Simulator only.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      container: z.string().describe('"data" or a group ID (e.g. "group.com.example")'),
      plist_path: z.string().describe("Relative path to the plist within the container"),
      key: z.string().describe("Plist key to remove"),
      udid: z.string().optional().describe("Simulator UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, container, plist_path, key, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id, container, plist_path, key };
      if (udid) body.udid = udid;
      const data = await apiRequest("DELETE", "/api/v1/device/app/state/plist/key", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("start_plist_watch", {
    description: `Start watching a plist file for changes. Polls the file on an interval and emits per-key changes as log entries into the same pipeline as app logs and proxy flows — visible in query_logs, tail_logs, and get_log_summary with source "plist_watcher".

Logs an initial snapshot of all keys when the watch starts. Then emits one log entry per added, removed, or changed key as mutations happen. Simulator only.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      container: z.string().describe('"data" or a group ID (e.g. "group.com.example")'),
      plist_path: z.string().describe("Relative path to the plist within the container"),
      poll_interval: z.coerce.number().default(1.0).describe("Poll interval in seconds (default 1.0)"),
      ignore_prefixes: z.array(z.string()).optional().describe('Key prefixes to ignore (e.g. ["kGSPSearchFilter", "LaunchDarkly"]). Changes to keys starting with these prefixes are silently dropped.'),
      udid: z.string().optional().describe("Simulator UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, container, plist_path, poll_interval, ignore_prefixes, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id, container, plist_path, poll_interval };
      if (ignore_prefixes) body.ignore_prefixes = ignore_prefixes;
      if (udid) body.udid = udid;
      const data = await apiRequest("POST", "/api/v1/device/app/state/plist/watch/start", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("stop_plist_watch", {
    description: `Stop watching a plist file for changes.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      container: z.string().describe('"data" or a group ID (e.g. "group.com.example")'),
      plist_path: z.string().describe("Relative path to the plist within the container"),
      udid: z.string().optional().describe("Simulator UDID (defaults to active device)"),
    }),
  }, async ({ bundle_id, container, plist_path, udid }) => {
    try {
      const body: Record<string, unknown> = { bundle_id, container, plist_path };
      if (udid) body.udid = udid;
      const data = await apiRequest("POST", "/api/v1/device/app/state/plist/watch/stop", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("configure_plist_watch", {
    description: `Save persistent plist watch config for an app. Supports multiple plists per bundle — e.g., app group plist for coaching flags + main container plist for SDK config, each with its own ignore list.

When start_simulator_logging runs, it automatically starts watchers for ALL configured targets. Config persists in ~/.quern/config.json across server restarts.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier"),
      watches: z.array(z.object({
        container: z.string().describe('"data" or a group ID (e.g. "group.com.example")'),
        plist_path: z.string().describe("Relative path to the plist within the container"),
        ignore_prefixes: z.array(z.string()).optional().describe('Key prefixes to ignore in this plist'),
      })).describe("List of plist targets to watch for this bundle"),
    }),
  }, async ({ bundle_id, watches }) => {
    try {
      const body: Record<string, unknown> = { bundle_id, watches };
      const data = await apiRequest("POST", "/api/v1/device/app/state/plist/watch/configure", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("get_plist_watch_config", {
    description: `Read the persistent plist watch configuration. Shows which apps have auto-start plist watching configured and their ignore prefixes.`,
    inputSchema: strictParams({}),
  }, async () => {
    try {
      const data = await apiRequest("GET", "/api/v1/device/app/state/plist/watch/config");
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });

  server.registerTool("unconfigure_plist_watch", {
    description: `Remove persistent plist watch config for an app. After this, start_simulator_logging will no longer auto-start a plist watcher for this bundle.`,
    inputSchema: strictParams({
      bundle_id: z.string().describe("App bundle identifier to remove config for"),
    }),
  }, async ({ bundle_id }) => {
    try {
      const body: Record<string, unknown> = { bundle_id };
      const data = await apiRequest("DELETE", "/api/v1/device/app/state/plist/watch/configure", undefined, body);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });
}
