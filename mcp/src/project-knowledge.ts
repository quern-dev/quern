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
import { connectionCode, NEVER_CONNECTED } from "./http.js";

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
  | { kind: "not_loaded"; reason: string }
  | { kind: "load_unknown"; reason: string }
  /** Not attempted: the server cannot read this machine's path, or this
   * key may not load the server-wide sets (#254, phase 3b). */
  | { kind: "skipped"; reason: string }
  /** Uploaded and loaded for this session alone (#560). */
  | { kind: "session_loaded"; app: string; screens: number };

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
  skip?: string,
): Promise<KnowledgeOutcome> {
  if (!pk.knowledgeDir) return { kind: "none" };
  if (pk.screenFiles === 0) return { kind: "empty" };
  if (skip) return { kind: "skipped", reason: skip };
  if (!pk.bundleId) {
    return { kind: "not_loaded", reason: "its .quern/config.json names no bundle_id" };
  }
  if (!reachable) return { kind: "not_loaded", reason: "the quern server was not reachable" };
  let loadSent = false;
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
    loadSent = true;
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
    const code = connectionCode(e);
    const message = e instanceof Error ? e.message : String(e);
    const reason = code && !message.includes(code) ? `${message} (${code})` : message;
    // A load that was sent and never answered may still finish: the server
    // does not cancel the scan when this side gives up, so "not loaded"
    // could be false a moment later (CodeRabbit). Two failures are answers
    // all the same: an HTTP status is the server's own refusal, and a
    // connection that was never made cannot have delivered anything.
    const answered = message.startsWith("HTTP ") || NEVER_CONNECTED.has(code ?? "");
    return loadSent && !answered
      ? { kind: "load_unknown", reason }
      : { kind: "not_loaded", reason };
  }
}

/** The knowledge directory a `load_landmarks` path names, as the server reads
 * one: a project root (its `.quern/knowledge`), a `.quern`, or the knowledge
 * directory itself -- and the app its project's config names. Null when the
 * path holds no `screens/`. */
export function knowledgeAt(path: string): { dir: string; bundleId: string | null } | null {
  const candidates: [string, string][] = [
    [join(path, ".quern", "knowledge"), join(path, ".quern")],
    [join(path, "knowledge"), path],
    [path, join(path, "..")],
    [join(path, ".."), join(path, "..", "..")],      // the screens/ folder itself
  ];
  for (const [dir, quern] of candidates) {
    if (!isDir(join(dir, "screens"))) continue;
    let bundleId: string | null = null;
    try {
      const parsed = JSON.parse(readFileSync(join(quern, "config.json"), "utf-8"));
      if (parsed && typeof parsed.bundle_id === "string" && parsed.bundle_id) {
        bundleId = parsed.bundle_id;
      }
    } catch {
      bundleId = null;
    }
    return { dir, bundleId };
  }
  return null;
}

/** Upload the project's knowledge base and load it for this session only
 * (#560): what a client key or a remote server gets instead of a path load.
 * `open` makes the session exist first -- the server loads it for a held
 * session, so it ends with it. Never throws. */
export async function uploadKnowledgeForSession(
  pk: ProjectKnowledge,
  reachable: boolean,
  open: () => Promise<boolean>,
  upload: (dir: string, app: string) => Promise<{ upload_id: string }>,
  request: Request,
): Promise<KnowledgeOutcome> {
  if (!pk.knowledgeDir) return { kind: "none" };
  if (pk.screenFiles === 0) return { kind: "empty" };
  if (!pk.bundleId) {
    return { kind: "not_loaded", reason: "its .quern/config.json names no bundle_id" };
  }
  if (!reachable) return { kind: "not_loaded", reason: "the quern server was not reachable" };
  try {
    if (!(await open())) {
      return { kind: "not_loaded", reason: "the session could not be opened to load it into" };
    }
    const uploaded = await upload(pk.knowledgeDir, pk.bundleId);
    const result = (await request("POST", "/api/v1/landmarks/load", undefined,
      { upload_id: uploaded.upload_id, app: pk.bundleId }, AUTOLOAD_TIMEOUT_MS)) as {
      screens?: number;
    };
    if (typeof result?.screens === "number" && result.screens > 0) {
      return { kind: "session_loaded", app: pk.bundleId, screens: result.screens };
    }
    return { kind: "not_loaded", reason: `the server answered ${JSON.stringify(result).slice(0, 200)}` };
  } catch (e) {
    return { kind: "not_loaded", reason: e instanceof Error ? e.message : String(e) };
  }
}

/** Load the knowledge base again for a session opened afresh after the old
 * one was lost (#560), and say so when that fails -- the instructions still
 * describe it as loaded. Never throws. */
export async function reloadForNewSession(
  pk: ProjectKnowledge,
  upload: (dir: string, app: string) => Promise<{ upload_id: string }>,
  request: Request,
  notify: (text: string) => void,
): Promise<KnowledgeOutcome> {
  const again = await uploadKnowledgeForSession(pk, true, async () => true, upload, request);
  if (again.kind !== "session_loaded") {
    const why = "reason" in again ? again.reason : again.kind;
    notify(`This session's knowledge base${pk.bundleId ? ` for ${pk.bundleId}` : ""} was ` +
      `lost with the session and could not be loaded again (${why}). identify_screen now ` +
      `uses the server's own sets; load_landmarks(path="${pk.projectDir}") loads it for ` +
      "this session again.");
  }
  return again;
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
    case "session_loaded":
      return ["", `${kb} -- uploaded to the quern server and loaded for this session only: ` +
        `${outcome.screens} screens for ${outcome.app}. identify_screen and ` +
        "get_screen_summary(identify=true) use them in place of any set the server has for " +
        `that app; other sessions are unaffected, and they end with this session. ${readIt}`];
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
    case "skipped":
      return ["", `${kb} -- not loaded: ${outcome.reason}. identify_screen names only the ` +
        `screens the server already has (list_landmarks shows them). ${readIt}`];
    case "load_unknown":
      return ["", `${kb} -- load sent but not confirmed: ${outcome.reason}. It may still ` +
        "finish on the server. Call list_landmarks to see whether it did, and " +
        `${loadCall} if it did not. ${readIt}`];
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
