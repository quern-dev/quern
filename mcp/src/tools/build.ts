import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerBuildTools(server: McpServer): void {
  server.registerTool("build_and_install", {
    description: `Build an app and install it on one or more devices: an Xcode scheme on iOS devices and simulators, or a Gradle project on Android devices and emulators.

ANDROID (a Gradle project: project_path is the build root or a module inside it). Pass variant (e.g. "debug", "stagingDebug"); module defaults to "app". Runs the project's own ./gradlew :module:assemble<Variant>, installs the APK on each Android target in parallel (the split matching each device's CPU, or the universal one), and records the build -- its R8 mapping and native libraries -- so its crashes are symbolicated by get_latest_crash without a record_android_build call. quern finds a JDK and the Android SDK itself, since a daemon started from the menu bar does not see your shell's JAVA_HOME or sdkman, and the response names the ones it used (java, android_sdk). Gradle's own daemon stays running between builds, as it does under Android Studio.

When the MACHINE rather than the code stops the build -- no suitable JDK, the toolchain JDK the build asks for, no Android SDK, missing SDK packages or licences, the NDK -- the response has an "environment" list instead of a build: each entry has a kind, a summary, what was found, and options, most direct first. Some options you can apply yourself by calling again: java_home="<a JDK it found>", or gradle_args=[...] (for example a toolchain path). Others -- installing a JDK or SDK package, editing gradle.properties or local.properties -- change the user's machine or project: ask the user before doing them. Never installs or edits anything itself.

A failed build gives Gradle's reason: compile errors with file and line (Kotlin, Java, resources), or what went wrong otherwise. An install refused because the installed app is signed with a different key says so; uninstall_on_signature_mismatch=true uninstalls it first, which ERASES the app's data on that device -- ask the user before passing it. One refused because the installed build has a higher versionCode says so too; allow_downgrade=true installs over it (debuggable builds only) and keeps the data.

iOS:

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
        "iOS: the .xcodeproj, .xcworkspace, or a directory containing one. Android: the " +
        "Gradle build's root (where settings.gradle is) or a module directory inside it."
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
      variant: z.string().optional().describe(
        "Android/Gradle: the build variant to assemble, e.g. \"debug\" or \"stagingDebug\" " +
        "(a build type with any flavour before it). Required for a Gradle project."
      ),
      module: z.string().optional().describe(
        "Android/Gradle: the module to build when project_path is the build root (default \"app\")."
      ),
      java_home: z.string().optional().describe(
        "Android/Gradle: a JDK to run Gradle with, typically one an environment problem listed."
      ),
      gradle_args: z.array(z.string()).optional().describe(
        "Android/Gradle: extra arguments for gradlew, e.g. one an environment problem suggested."
      ),
      uninstall_on_signature_mismatch: z.union([
        z.boolean(),
        z.enum(["true", "false"]).transform((v) => v === "true"),
      ]).optional().describe(
        "Android: if an install is refused because the installed app has a different signing " +
        "key, uninstall it and install again. ERASES the app's data on that device: ask first."
      ),
      allow_downgrade: z.union([
        z.boolean(),
        z.enum(["true", "false"]).transform((v) => v === "true"),
      ]).optional().describe(
        "Android: install over an installed build with a higher versionCode (adb install -d). " +
        "Android allows it for a debuggable build only; the app's data is kept."
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
  }, async ({ project_path, scheme, udids, configuration, skip_plugin_validation,
               variant, module, java_home, gradle_args, uninstall_on_signature_mismatch,
               allow_downgrade }) => {
    try {
      const body: Record<string, unknown> = { project_path, configuration };
      if (skip_plugin_validation) body.skip_plugin_validation = true;
      if (variant) body.variant = variant;
      if (module) body.module = module;
      if (java_home) body.java_home = java_home;
      if (gradle_args && gradle_args.length > 0) body.gradle_args = gradle_args;
      if (uninstall_on_signature_mismatch) body.uninstall_on_signature_mismatch = true;
      if (allow_downgrade) body.allow_downgrade = true;
      if (scheme) body.scheme = scheme;
      if (udids && udids.length > 0) body.udids = udids;

      // A build answers when it is done, which can be well past fetch's
      // 300s wait for headers; the server bounds it instead.
      const data = await apiRequest(
        "POST",
        "/api/v1/device/build-and-install",
        undefined,
        body,
        "none"
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
    description: `Record an Android build so its crashes can be symbolicated. quern does not run Gradle: build the variant yourself (./gradlew assembleStagingRelease), then call this with the app module's directory and the variant, right after the build: it records whatever build/ holds, and says when that is more than an hour old.

quern keeps copies of what symbolicating needs, because the next build overwrites them: the APK's package, versionName and versionCode; R8's mapping.txt and its pg_map_id for a minified variant (not kept when the APK's own R8 marker names another mapping); and the unstripped native libraries from merged_native_libs, by the BuildId a tombstone names them by. get_latest_crash then retraces a minified build's Java frames with retrace (Android SDK command-line tools) and resolves native frames with the NDK's llvm-symbolizer. Records follow the same retention as iOS builds: symbols for the newest 10 builds per variant, records for 30 days.

Returns the record and a one-line summary. A variant with no APK output is a 404 that names the variants that were built.`,
    inputSchema: strictParams({
      module_path: z.string().describe(
        "The app module's absolute directory: the one with build.gradle(.kts) and build/, e.g. /path/to/project/app"
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
