import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerBuildTools(server: McpServer): void {
  server.registerTool("build_and_install", {
    description: `Build an Xcode scheme and install the resulting app on one or more devices or simulators.

Builds once per required architecture — not once per device:
- Physical devices  → generic/platform=iOS        (one build, installed on all physical targets)
- Simulators        → generic/platform=iOS Simulator (one build, installed on all simulator targets)

Both architectures are built concurrently when the target list mixes physical and simulator devices.

Handles UDID resolution automatically — pass Quern device UDIDs from list_devices and Quern
will resolve the correct xcodebuild destination format (including CoreDevice UUID → hardware
UDID translation for physical iOS 17+ devices).

If scheme is omitted, returns an error listing all available schemes in the project.
Pick one and call again.

Pre-install check: if the device OS is below the app's MinimumOSVersion, that device is
skipped with a clear error rather than a cryptic installer failure.

Returns per-device install results plus per-architecture build results.`,
    inputSchema: strictParams({
      project_path: z.string().describe(
        "Path to the .xcodeproj, .xcworkspace, or a directory containing one."
      ),
      scheme: z.string().optional().describe(
        "Build scheme name. If omitted, returns an error listing available schemes."
      ),
      udids: z.array(z.string()).optional().describe(
        "Device UDIDs from list_devices. Accepts multiple targets — builds once per " +
        "required architecture and installs in parallel. If omitted, uses the active/auto-detected device."
      ),
      configuration: z.string().optional().default("Debug").describe(
        "Build configuration (default: Debug)"
      ),
    }),
  }, async ({ project_path, scheme, udids, configuration }) => {
    try {
      const body: Record<string, unknown> = { project_path, configuration };
      if (scheme) body.scheme = scheme;
      if (udids && udids.length > 0) body.udids = udids;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/build-and-install",
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
}
