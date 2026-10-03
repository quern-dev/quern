/**
 * The app knowledge base of the project an agent is working in.
 *
 * The quern server cannot know which directory an agent is in; this process
 * can -- the agent's client starts it there. So this is where a project's
 * `.quern/knowledge` is found and loaded at session start, and where an app
 * project without one is noticed, so the agent can suggest building one.
 *
 * The session's instructions are written after the load, from its outcome:
 * a load that did not happen is never described as one that did. Everything
 * here is best-effort, and nothing here can stop the MCP server starting.
 */

import { existsSync, readdirSync, readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

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

/** What became of the knowledge base at session start. */
export type KnowledgeOutcome =
  | { kind: "none" }
  | { kind: "empty" }
  | { kind: "loaded"; app: string; screens: number }
  | { kind: "already"; app: string; screens: number }
  | { kind: "other"; app: string; from: string }
  | { kind: "not_loaded"; reason: string };

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

/** An Android app module: some module of the build has a manifest. A bare
 * settings.gradle is any JVM build -- a Spring service, a library. */
function hasAndroidModule(dir: string): boolean {
  if (existsSync(join(dir, "src", "main", "AndroidManifest.xml"))) return true;
  return entries(dir).some((e) => existsSync(join(dir, e, "src", "main", "AndroidManifest.xml")));
}

/** Why `dir` looks like a mobile app project, or null. Deliberately narrow:
 * a nudge in every repo -- a backend, docs, quern itself -- would teach
 * agents to ignore it. */
export function appProjectMarker(dir: string): string | null {
  const here = entries(dir);
  const xcode = here.find((e) => e.endsWith(".xcodeproj") || e.endsWith(".xcworkspace"));
  if (xcode) return `Xcode project (${xcode})`;
  const gradle = here.find((e) => e === "settings.gradle" || e === "settings.gradle.kts");
  if (gradle && hasAndroidModule(dir)) return `Android project (${gradle})`;
  if (here.includes("pubspec.yaml") && (here.includes("ios") || here.includes("android"))) {
    return "Flutter project (pubspec.yaml)";
  }
  // React Native and similar: the native projects one level down.
  const nested = entries(join(dir, "ios")).find((e) => e.endsWith(".xcodeproj"));
  if (nested) return `iOS project (ios/${nested})`;
  const androidGradle = ["settings.gradle", "settings.gradle.kts"].find((f) =>
    existsSync(join(dir, "android", f)));
  if (androidGradle && hasAndroidModule(join(dir, "android"))) {
    return `Android project (android/${androidGradle})`;
  }
  return null;
}

/** QUERN_PROJECT_DIR as a path: `~` expanded, relative made absolute. */
export function projectDirFromEnv(value: string | undefined, cwd: string): string {
  if (!value) return cwd;
  const expanded = value === "~" || value.startsWith("~/")
    ? join(homedir(), value.slice(1))
    : value;
  return resolve(cwd, expanded);
}

export function detectProjectKnowledge(
  dir: string = projectDirFromEnv(process.env.QUERN_PROJECT_DIR, process.cwd()),
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

type Request = (
  method: "GET" | "POST",
  path: string,
  params: undefined,
  body: unknown,
  timeoutMs: number,
) => Promise<unknown>;

/** Load timeout: a reachable server scans a local directory in well under it,
 * and the session waits for it before it can start. */
export const AUTOLOAD_TIMEOUT_MS = 5000;

/** Symlinks followed: one checkout reached through a linked path is still
 * one checkout, and calling it "another" would leave this session unloaded. */
function samePlace(a: string, b: string): boolean {
  const real = (p: string) => {
    try {
      return realpathSync(p);
    } catch {
      return resolve(p);
    }
  };
  return real(a) === real(b);
}

/** Load the project's knowledge base into the running server, unless it is
 * empty, unnamed, already loaded, or another checkout's set for the same app
 * is -- which is said, not replaced: sessions in two worktrees share one
 * server. Never throws. */
export async function autoloadKnowledge(
  pk: ProjectKnowledge,
  request: Request,
  reachable: boolean,
): Promise<KnowledgeOutcome> {
  if (!pk.knowledgeDir) return { kind: "none" };
  if (pk.screenFiles === 0) return { kind: "empty" };
  if (!pk.bundleId) {
    return { kind: "not_loaded", reason: "its .quern/config.json names no bundle_id" };
  }
  if (!reachable) return { kind: "not_loaded", reason: "the quern server was not reachable" };
  try {
    const listing = (await request("GET", "/api/v1/landmarks/", undefined, undefined,
      AUTOLOAD_TIMEOUT_MS)) as { sets?: Record<string, number>; sources?: Record<string, string> };
    const loaded = listing?.sets?.[pk.bundleId];
    const from = listing?.sources?.[pk.bundleId];
    if (typeof loaded === "number" && loaded > 0) {
      // A server too old to say where a set came from cannot tell us it is
      // ours, so it is read as someone else's: kept, not loaded over.
      return from && samePlace(from, pk.knowledgeDir)
        ? { kind: "already", app: pk.bundleId, screens: loaded }
        : { kind: "other", app: pk.bundleId, from: from || "a path this quern does not report" };
    }
    const result = (await request("POST", "/api/v1/landmarks/load", undefined,
      { source: pk.knowledgeDir, app: pk.bundleId }, AUTOLOAD_TIMEOUT_MS)) as {
      screens?: number;
      detail?: unknown;
    };
    if (typeof result?.screens === "number" && result.screens > 0) {
      return { kind: "loaded", app: pk.bundleId, screens: result.screens };
    }
    return { kind: "not_loaded", reason: `the server answered ${JSON.stringify(result).slice(0, 200)}` };
  } catch (e) {
    return { kind: "not_loaded", reason: e instanceof Error ? e.message : String(e) };
  }
}

/** Lines for the session instructions, from what actually happened. Nothing
 * at all for a directory that is neither a knowledge base nor an app project. */
export function knowledgeInstructions(pk: ProjectKnowledge, outcome: KnowledgeOutcome): string[] {
  const loadCall = `load_landmarks(path="${pk.projectDir}"${pk.bundleId ? "" : `, app="<bundle id>"`})`;
  const readIt = "Read its README.md and app.md for the app's flows, deep links and quirks " +
    "before exploring by hand.";
  const kb = `APP KNOWLEDGE BASE: ${pk.knowledgeDir}`;
  switch (outcome.kind) {
    case "loaded":
      return ["", `${kb} -- loaded ${outcome.screens} screens for ${outcome.app} at the start of ` +
        "this session, so identify_screen and get_screen_summary(identify=true) can name " +
        `screens. ${readIt}`];
    case "already":
      return ["", `${kb} -- quern already had its ${outcome.screens} screens for ${outcome.app} ` +
        `loaded. ${readIt}`];
    case "other":
      return ["", `${kb} -- quern has ${outcome.app}'s landmarks loaded from ${outcome.from}, ` +
        "another checkout, and they were left in place: another session may be using them. To " +
        `identify against this checkout's instead, call ${loadCall}. ${readIt}`];
    case "not_loaded":
      return ["", `${kb} -- NOT loaded: ${outcome.reason}. identify_screen will find nothing ` +
        `until you call ${loadCall}` + (pk.bundleId ? "" : " with the app's bundle id") +
        ` (after ensure_server, if the server was down). ${readIt}`];
    case "empty":
      return ["", `${kb} -- started, but it has no screens yet. When identifying screens would ` +
        "help the task, suggest filling it in to the user -- do not start it unasked: the " +
        "quern://app-knowledge-guide resource walks through it as a guided tour."];
    case "none":
      if (!pk.appMarker) return [];
      return ["", `APP KNOWLEDGE BASE: none in this project (${pk.projectDir}), which looks ` +
        `like an app project: ${pk.appMarker}.`,
        "A knowledge base lets quern identify screens by name and records the app's flows, " +
        "deep links and quirks for every later session. When that would help the task, " +
        "suggest building one to the user -- do not start it unasked: init_app_knowledge and " +
        "the quern://app-knowledge-guide resource walk through it as a guided tour. One kept " +
        "elsewhere may already be loaded: list_landmarks shows what is, including any " +
        "remembered at every start."];
  }
}
