import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";

export function registerWdaTools(server: McpServer): void {
  server.tool(
    "setup_wda",
    `Set up WebDriverAgent on a physical iOS device for UI automation. Discovers signing identities, clones the WDA repo, builds, and installs. If multiple signing identities exist and no team_id is provided, returns the list for you to choose from — call again with the chosen team_id.`,
    {
      udid: z.string().describe("Physical device UDID"),
      team_id: z
        .string()
        .optional()
        .describe(
          "Apple Developer Team ID for code signing. Required when multiple signing identities exist."
        ),
    },
    async ({ udid, team_id }) => {
      try {
        const body: Record<string, unknown> = { udid };
        if (team_id) body.team_id = team_id;

        const data = await apiRequest(
          "POST",
          "/api/v1/device/wda/setup",
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
}
