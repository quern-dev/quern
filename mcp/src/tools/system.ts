import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerSystemTools(server: McpServer): void {
  server.registerTool(
    "update_quern",
    {
      description:
        `Update Quern to the latest release on the user's configured channel (stable or beta). Launches the equivalent of "quern update" — pulls the latest source (or fetches the release tarball), reinstalls Python deps, rebuilds the MCP server, and restarts the daemon. The HTTP call returns immediately; the actual upgrade runs in a detached child and takes ~30-60 seconds. The Quern server will restart during that window, so the next MCP tool call may fail until it's back up. Reconnect by retrying ensure_server. Only call this after the user has confirmed they want to update — typically prompted by an "update_available" hint in ensure_server's response.`,
      inputSchema: strictParams({}),
    },
    async () => {
      try {
        const data = await apiRequest("POST", "/api/v1/system/update");
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
              text: `Error launching update: ${e instanceof Error ? e.message : String(e)}\n\nFall back to running "quern update" in a terminal manually.`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  server.registerTool(
    "set_update_channel",
    {
      description:
        `Set the user's Quern update channel preference (stable or beta). "stable" tracks the release/stable branch (only tagged releases); "beta" tracks release/beta for opt-in early testing. Setting the channel only updates ~/.quern/config.json — it does not switch git branches or apply an update. After flipping to beta, the next call to update_quern (and any future periodic check) will compare against the beta branch. Only call this after the user has explicitly asked to change channels.`,
      inputSchema: strictParams({
        channel: z.enum(["stable", "beta"]).describe(
          "stable (default, recommended) or beta (opt-in prerelease)",
        ),
      }),
    },
    async ({ channel }) => {
      try {
        const data = await apiRequest(
          "PUT",
          "/api/v1/system/channel",
          undefined,
          { channel },
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
              text: `Error setting channel: ${e instanceof Error ? e.message : String(e)}`,
            },
          ],
          isError: true,
        };
      }
    },
  );
}
