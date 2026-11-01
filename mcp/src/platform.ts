/**
 * Which platform a device request is for (#582) -- the agent's side.
 *
 * The server holds the rule (server/device/platform.py): a platform the
 * request names restricts; one sent with a source is a default it may cross
 * for a single match on the other platform; otherwise a bare version both
 * platforms have is refused with each side listed. Only this process can see
 * the agent's project, so this is where its platform is read and sent.
 */

import { detectProjectPlatform, type Platform, type ProjectPlatform } from "./project-knowledge.js";

export type { Platform };

/** Read each time: init_app_knowledge, or the user, may set it mid-session. */
export function projectPlatform(): ProjectPlatform | null {
  try {
    return detectProjectPlatform();
  } catch {
    return null;
  }
}

/** The platform fields for a device request: the agent's own `platform`, as
 * a restriction, or else the project's, with where it came from. */
export function platformFields(
  asked: Platform | undefined,
  project: ProjectPlatform | null = projectPlatform(),
): { platform?: Platform; platform_source?: string } {
  if (asked) return { platform: asked };
  return project ? { platform: project.platform, platform_source: project.source } : {};
}

export const PLATFORM_DESCRIPTION =
  "'ios' or 'android'. Default: this project's platform -- .quern/config.json's \"platform\", " +
  "else read from the project (an Xcode or Android project) -- which a request may still cross " +
  "when exactly one device on the other platform matches it. With neither, a bare os_version " +
  "both platforms have ('16') is refused with the matches on each side. 'Android 16' or " +
  "'iOS 16' in os_version names the platform too. The answer says which platform was used " +
  "(platform, platform_source).";

const OS = /^\s*(ios|android)?\s*v?(\d+(?:\.\d+)*)?\s*$/i;

/** As the server's parse_os: the platform a version names, and its number. */
export function parseOs(requested: string | undefined): { platform?: Platform; version?: string } {
  if (!requested) return {};
  const m = requested.match(OS);
  if (m) {
    return {
      ...(m[1] ? { platform: m[1].toLowerCase() as Platform } : {}),
      ...(m[2] ? { version: m[2] } : {}),
    };
  }
  const number = requested.match(/\d+(?:\.\d+)*/)?.[0];
  return number ? { version: number } : {};
}

export function platformOf(deviceType: unknown): Platform | undefined {
  if (deviceType === "simulator" || deviceType === "device") return "ios";
  if (deviceType === "android_emulator" || deviceType === "android_device") return "android";
  return undefined;
}

/** As the server's os_matches: the number as a prefix, and a named platform
 * only on that platform -- "Android 14" is not iOS 14. */
export function osMatches(have: unknown, haveType: unknown, wanted: string | undefined): boolean {
  const { platform, version } = parseOs(wanted);
  if (platform && platformOf(haveType) !== platform) return false;
  if (!version) return !!platform;
  const v = String(have ?? "").match(/\d+(?:\.\d+)*/)?.[0];
  return !!v && (v === version || v.startsWith(version + "."));
}
