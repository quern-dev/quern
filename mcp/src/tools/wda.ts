import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerWdaTools(server: McpServer): void {
  server.registerTool("setup_wda", {
    description: `Set up WebDriverAgent on a physical iOS device or a simulator.

On a physical device: discovers signing identities, clones the WDA repo, builds, and installs. If multiple signing identities exist and no team_id is provided, returns the list for you to choose from — call again with the chosen team_id.

On a simulator: builds WDA for the iOS Simulator — no signing team, no provisioning, nothing to install. Optional: start_driver builds it on first use anyway. team_id is ignored.`,
    inputSchema: strictParams({
      udid: z.string().describe("Physical device or simulator UDID"),
      team_id: z
        .string()
        .optional()
        .describe(
          "Apple Developer Team ID for code signing. Required when multiple signing identities exist."
        ),
      force_rebuild: z
        .coerce.boolean()
        .optional()
        .describe(
          "Force a fresh WDA build even if one already exists for this team."
        ),
    }),
  }, async ({ udid, team_id, force_rebuild }) => {
    try {
      const body: Record<string, unknown> = { udid };
      if (team_id) body.team_id = team_id;
      if (force_rebuild) body.force = true;

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
  });

  server.registerTool("start_driver", {
    description: `Start the WDA driver (xcodebuild test-without-building) on a physical iOS device or a simulator. Returns status, PID, and whether WDA is responsive. The driver persists across server restarts.

ON A SIMULATOR THIS CHANGES WHAT YOU SEE. Simulators are normally read through the accessibility tree (sim-bridge/idb), and XCUITest does not classify elements the same way: a tab-bar item the accessibility tree calls RadioButton is XCUIElementTypeButton to XCUITest, so a selector written from get_ui_tree can fail at runtime with nothing explaining why. Once WDA answers, every UI read and action on that simulator goes through WDA until stop_driver, and elements read from it carry xcui_type — XCUITest's own type, the name to write a selector from. "backend" on UI responses says "wda". Use it when writing or debugging XCUITests; otherwise leave simulators on the default, which is faster. The first start builds WDA for the simulator, which takes a few minutes. If WDA does not answer, the simulator stays on sim-bridge and the response says why.`,
    inputSchema: strictParams({
      udid: z.string().describe("Physical device or simulator UDID"),
    }),
  }, async ({ udid }) => {
    try {
      const data = await apiRequest(
        "POST",
        "/api/v1/device/wda/start",
        undefined,
        { udid }
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
  });

  server.registerTool("stop_driver", {
    description: `Stop the WDA driver on a physical iOS device or a simulator. Deletes the active WDA session and kills the xcodebuild process. On a simulator, its UI goes back to the default backend (reported as "backend"); the first read afterwards may take a second longer while the accessibility bridge recovers from WDA.`,
    inputSchema: strictParams({
      udid: z.string().describe("Physical device or simulator UDID"),
    }),
  }, async ({ udid }) => {
    try {
      const data = await apiRequest(
        "POST",
        "/api/v1/device/wda/stop",
        undefined,
        { udid }
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
  });
}
