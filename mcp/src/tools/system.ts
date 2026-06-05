import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerSystemTools(server: McpServer): void {
  server.registerTool(
    "update_quern",
    {
      description:
        `Update Quern to the latest release. Launches the equivalent of "quern update" — pulls the latest source (or fetches the release tarball), reinstalls Python deps, rebuilds the MCP server, and restarts the daemon. The HTTP call returns immediately; the actual upgrade runs in a detached child and takes ~30-60 seconds. The Quern server will restart during that window, so the next MCP tool call may fail until it's back up. Reconnect by retrying ensure_server. Only call this after the user has confirmed they want to update — typically prompted by an "update_available" hint in ensure_server's response.`,
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
}
