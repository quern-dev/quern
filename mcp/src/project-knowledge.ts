/**
 * The app knowledge base of the project an agent is working in.
 *
 * The quern server cannot know which directory an agent is in; this process
 * can -- the agent's client starts it there. So this is where a project's
 * `.quern/knowledge` is found and loaded at session start, and where an app
 * project without one is noticed, so the agent can suggest building one.
 *
 * Everything here is best-effort and says what it did: a knowledge base that
 * could not be loaded is named in the instructions with the call to load it,
 * and nothing here can stop the MCP server starting.
 */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

export interface ProjectKnowledge {
  /** The directory looked in: QUERN_PROJECT_DIR, or this process's cwd. */
  projectDir: string;
  /** `<projectDir>/.quern/knowledge`, when it holds a `screens/` folder. */
  knowledgeDir: string | null;
  /** Screen files in it, not counting `_` templates. */
  screenFiles: number;
  /** `bundle_id` from `.quern/config.json`, if it names one. */
  bundleId: string | null;
  /** Why the directory reads as an app project, or null if it does not. */
  appMarker: string | null;
}

function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

function entries(dir: string): string[] {
  try {
    return readdirSync(dir);
  } catch {
    return [];
  }
}

/** Why `dir` looks like a mobile app project, or null. Deliberately narrow:
 * a nudge in every repo -- a backend, docs, quern itself -- would teach
 * agents to ignore it. */
export function appProjectMarker(dir: string): string | null {
  const here = entries(dir);
  const xcode = here.find((e) => e.endsWith(".xcodeproj") || e.endsWith(".xcworkspace"));
  if (xcode) return `Xcode project (${xcode})`;
  const gradle = here.find((e) => e === "settings.gradle" || e === "settings.gradle.kts");
  if (gradle) return `Gradle project (${gradle})`;
  if (here.includes("pubspec.yaml") && (here.includes("ios") || here.includes("android"))) {
    return "Flutter project (pubspec.yaml)";
  }
  // React Native and similar: the native projects one level down.
  const nested = entries(join(dir, "ios")).find((e) => e.endsWith(".xcodeproj"));
  if (nested) return `iOS project (ios/${nested})`;
  if (existsSync(join(dir, "android", "settings.gradle")) ||
      existsSync(join(dir, "android", "settings.gradle.kts"))) {
    return "Android project (android/settings.gradle)";
  }
  return null;
}

export function detectProjectKnowledge(
  dir: string = process.env.QUERN_PROJECT_DIR || process.cwd(),
): ProjectKnowledge {
  const quern = join(dir, ".quern");
  const knowledge = join(quern, "knowledge");
  const screens = join(knowledge, "screens");
  let bundleId: string | null = null;
  try {
    const parsed = JSON.parse(readFileSync(join(quern, "config.json"), "utf-8"));
    if (parsed && typeof parsed.bundle_id === "string" && parsed.bundle_id) {
      bundleId = parsed.bundle_id;
    }
  } catch {
    bundleId = null;
  }
  const hasKnowledge = isDir(screens);
  return {
    projectDir: dir,
    knowledgeDir: hasKnowledge ? knowledge : null,
    screenFiles: hasKnowledge
      ? entries(screens).filter((e) => e.endsWith(".md") && !e.startsWith("_")).length
      : 0,
    bundleId,
    appMarker: appProjectMarker(dir),
  };
}

/** Lines for the session instructions: what this project has, or -- for an
 * app project with none -- the suggestion to build one. Nothing at all for a
 * directory that is not an app project. */
export function knowledgeInstructions(pk: ProjectKnowledge): string[] {
  if (pk.knowledgeDir) {
    const app = pk.bundleId ?? "the app (no bundle_id in .quern/config.json -- pass app)";
    return [
      "",
      `APP KNOWLEDGE BASE: ${pk.knowledgeDir} -- ${pk.screenFiles} screen files for ${app}.`,
      "Quern loads its landmarks at the start of this session, so identify_screen and " +
        "get_screen_summary(identify=true) can name screens. If they report " +
        `no_landmarks_loaded, call load_landmarks(path="${pk.projectDir}"). Read its ` +
        "README.md and app.md for the app's flows, deep links and quirks before exploring by hand.",
    ];
  }
  if (pk.appMarker) {
    return [
      "",
      `APP KNOWLEDGE BASE: none in this project (${pk.projectDir}), which looks like an app ` +
        `project: ${pk.appMarker}.`,
      "A knowledge base lets quern identify screens by name and records the app's flows, " +
        "deep links and quirks for every later session. Suggest building one to the user -- " +
        "do not start it unasked: init_app_knowledge and the quern://app-knowledge-guide " +
        "resource walk through it as a guided tour. A knowledge base kept elsewhere may already " +
        "be loaded: list_landmarks shows what is, including any remembered at every start.",
    ];
  }
  return [];
}

type Request = (
  method: "POST",
  path: string,
  params: undefined,
  body: unknown,
  timeoutMs: number,
) => Promise<unknown>;

/** Load the project's knowledge base into the running server. Returns what
 * happened, for the log; never throws. */
export async function autoloadKnowledge(
  pk: ProjectKnowledge,
  request: Request,
): Promise<string> {
  if (!pk.knowledgeDir) return "no knowledge base in this project";
  try {
    const body: Record<string, unknown> = { source: pk.projectDir };
    if (pk.bundleId) body.app = pk.bundleId;
    const result = (await request("POST", "/api/v1/landmarks/load", undefined, body, 5000)) as {
      loaded?: string;
      screens?: number;
      detail?: string;
    };
    if (result && typeof result.screens === "number") {
      return `loaded ${result.screens} screens for ${result.loaded} from ${pk.knowledgeDir}`;
    }
    return `could not load ${pk.knowledgeDir}: ${JSON.stringify(result).slice(0, 200)}`;
  } catch (e) {
    return `could not load ${pk.knowledgeDir}: ${e instanceof Error ? e.message : String(e)}`;
  }
}
