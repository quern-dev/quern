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

Returns per-device install results plus per-architecture build results. Each successful build
is recorded, and the summary names the record: bundle id, version, configuration and every
binary's UUID, which is how a crash report names what it ran. For a device build, dSYMs are kept
too -- made for the app's own code, copied where Xcode or a vendor made one -- for the newest 10
device builds per scheme, so a crash from this build can be symbolicated after later builds
overwrite DerivedData. Each binary's \`dwarf\` is the file inside its dSYM to pass to
\`atos -o\`: one dSYM can cover several binaries, and atos given the bundle resolves nothing.

A failed build names its cause, including failures outside compilation (signing, provisioning).
A project whose Swift package plug-in or macro has not been approved in Xcode is refused by
xcodebuild; the error says so, and skip_plugin_validation=true builds anyway.`,
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
      skip_plugin_validation: z.union([
        z.boolean(),
        z.enum(["true", "false"]).transform((v) => v === "true"),
      ]).optional().describe(
        "Build even if a Swift package plug-in or macro has not been approved in Xcode " +
        "(adds -skipPackagePluginValidation and -skipMacroValidation). Off by default: " +
        "approving one is a trust decision. A build refused for this says so in its errors."
      ),
    }),
  }, async ({ project_path, scheme, udids, configuration, skip_plugin_validation }) => {
    try {
      const body: Record<string, unknown> = { project_path, configuration };
      if (skip_plugin_validation) body.skip_plugin_validation = true;
      if (scheme) body.scheme = scheme;
      if (udids && udids.length > 0) body.udids = udids;

      const data = await apiRequest(
        "POST",
        "/api/v1/device/build-and-install",
        undefined,
        body
      );

      // Return concise summary on full success, full JSON on any failure
      const resp = data as Record<string, unknown>;
      if (resp.all_installed && resp.summary) {
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("record_android_build", {
    description: `Record an Android build so its crashes can be symbolicated. quern does not run Gradle: build the variant yourself (./gradlew assembleStagingRelease), then call this with the app module's directory and the variant.

quern keeps copies of what symbolicating needs, because the next build overwrites them: the APK's package, versionName and versionCode; R8's mapping.txt and its pg_map_id for a minified variant; and the unstripped native libraries from merged_native_libs, by the BuildId a tombstone names them by. get_latest_crash then retraces a minified build's Java frames with retrace (Android SDK command-line tools) and resolves native frames with the NDK's llvm-symbolizer. Records follow the same retention as iOS builds: symbols for the newest 10 builds per variant, records for 30 days.

Returns the record and a one-line summary. A variant with no APK output is a 404 that names the variants that were built.`,
    inputSchema: strictParams({
      module_path: z.string().describe(
        "The app module's directory: the one with build.gradle(.kts) and build/, e.g. /path/to/project/app"
      ),
      variant: z.string().describe(
        "The variant built, e.g. stagingRelease, prodDebug"
      ),
    }),
  }, async ({ module_path, variant }) => {
    try {
      const data = await apiRequest("POST", "/api/v1/builds/android/record", undefined,
                                    { module_path, variant }) as Record<string, unknown>;
      return {
        content: [{ type: "text" as const, text: (data.summary as string) + "\n\n" + JSON.stringify(data.record, null, 2) }],
      };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
        isError: true,
      };
    }
  });
}
