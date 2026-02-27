import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";

export function registerAppStateTools(server: McpServer): void {
  // ---------------------------------------------------------------------------
  // Checkpoint tools
  // ---------------------------------------------------------------------------

  server.tool(
    "save_app_state",
    `Save a named checkpoint of a simulator app's state. Captures the data container and all app group containers. Terminates the app before copying. Simulator only.

Use this to snapshot known-good states (e.g. "logged_in", "staging_configured") so you can restore them later without reinstalling.`,
    {
      bundle_id: z.string().describe("App bundle identifier (e.g. com.example.MyApp)"),
      label: z.string().describe('Short name for this checkpoint (e.g. "logged_in", "fresh_install")'),
      description: z.string().optional().describe("Human-readable description of this state"),
      udid: z.string().optional().describe("Simulator UDID (auto-resolves if omitted)"),
    },
    async ({ bundle_id, label, description, udid }) => {
      try {
        const body: Record<string, unknown> = { bundle_id, label };
        if (description) body.description = description;
        if (udid) body.udid = udid;
        const data = await apiRequest("POST", "/api/v1/device/app/state/save", undefined, body);
        return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
      } catch (e) {
        return {
          content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "restore_app_state",
    `Restore a named app state checkpoint. Terminates the app, wipes live container contents, and copies the checkpoint back using re-resolved live paths (handles UUID rotation on reinstall). Simulator only.`,
    {
      bundle_id: z.string().describe("App bundle identifier"),
      label: z.string().describe("Name of the checkpoint to restore"),
      udid: z.string().optional().describe("Simulator UDID (auto-resolves if omitted)"),
    },
    async ({ bundle_id, label, udid }) => {
      try {
        const body: Record<string, unknown> = { bundle_id, label };
        if (udid) body.udid = udid;
        const data = await apiRequest("POST", "/api/v1/device/app/state/restore", undefined, body);
        return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
      } catch (e) {
        return {
          content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "list_app_states",
    `List all saved state checkpoints for an app. Returns metadata including label, description, and capture timestamp.`,
    {
      bundle_id: z.string().describe("App bundle identifier"),
    },
    async ({ bundle_id }) => {
      try {
        const data = await apiRequest("GET", "/api/v1/device/app/state/list", { bundle_id });
        return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
      } catch (e) {
        return {
          content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
          isError: true,
        };
      }
    }
  );

  server.tool(
    "delete_app_state",
    `Delete a named app state checkpoint.`,
    {
      bundle_id: z.string().describe("App bundle identifier"),
      label: z.string().describe("Name of the checkpoint to delete"),
    },
    async ({ bundle_id, label }) => {
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
    }
  );

  // ---------------------------------------------------------------------------
  // Plist tools
  // ---------------------------------------------------------------------------

  server.tool(
    "read_app_plist",
    `Read a plist file (or single key) from a simulator app's container. Useful for inspecting feature flags, cached tokens, or any preference stored in a plist.

Container can be "data" (main data container) or a group ID like "group.com.example".
plist_path is relative to the container root (e.g. "Library/Preferences/com.example.plist").
If key is omitted, returns the entire plist as JSON.`,
    {
      bundle_id: z.string().describe("App bundle identifier"),
      container: z.string().describe('"data" or a group ID (e.g. "group.com.example")'),
      plist_path: z.string().describe("Relative path to the plist within the container"),
      key: z.string().optional().describe("Specific key to read (omit to return entire plist)"),
      udid: z.string().optional().describe("Simulator UDID (auto-resolves if omitted)"),
    },
    async ({ bundle_id, container, plist_path, key, udid }) => {
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
    }
  );

  server.tool(
    "set_app_plist_value",
    `Set a key in a plist file inside a simulator app's container. More surgical than a full state restore — flip a single feature flag without touching anything else. Simulator only.

Type inference: boolean values set -bool, integers set -integer, floats set -float, everything else -string.`,
    {
      bundle_id: z.string().describe("App bundle identifier"),
      container: z.string().describe('"data" or a group ID (e.g. "group.com.example")'),
      plist_path: z.string().describe("Relative path to the plist within the container"),
      key: z.string().describe("Plist key to set"),
      value: z.union([z.string(), z.number(), z.boolean()]).describe("Value to set (type is inferred)"),
      udid: z.string().optional().describe("Simulator UDID (auto-resolves if omitted)"),
    },
    async ({ bundle_id, container, plist_path, key, value, udid }) => {
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
    }
  );

  server.tool(
    "delete_app_plist_key",
    `Remove a key from a plist file inside a simulator app's container. Useful for simulating missing/unset state (e.g. remove a flag to trigger first-launch flow). Simulator only.`,
    {
      bundle_id: z.string().describe("App bundle identifier"),
      container: z.string().describe('"data" or a group ID (e.g. "group.com.example")'),
      plist_path: z.string().describe("Relative path to the plist within the container"),
      key: z.string().describe("Plist key to remove"),
      udid: z.string().optional().describe("Simulator UDID (auto-resolves if omitted)"),
    },
    async ({ bundle_id, container, plist_path, key, udid }) => {
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
    }
  );
}
