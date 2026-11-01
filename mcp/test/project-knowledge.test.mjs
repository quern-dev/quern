// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, symlinkSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  AUTOLOAD_TIMEOUT_MS,
  appProjectMarker,
  autoloadKnowledge,
  detectProjectKnowledge,
  knowledgeInstructions,
  projectDirFromEnv,
  knowledgeAt,
  reloadForNewSession,
  uploadKnowledgeForSession,
} from "../dist/project-knowledge.js";

const APP = "com.example.app";
const MANIFEST = "<manifest/>";

function project({ knowledge = false, bundle = null, files = {} } = {}) {
  // Real, because the spawned server sees its cwd resolved: /private/var, not /var.
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "quern-pk-")));
  for (const [path, body] of Object.entries(files)) {
    mkdirSync(join(dir, path, ".."), { recursive: true });
    if (body === "dir") mkdirSync(join(dir, path), { recursive: true });
    else writeFileSync(join(dir, path), body);
  }
  if (knowledge) {
    mkdirSync(join(dir, ".quern", "knowledge", "screens"), { recursive: true });
    writeFileSync(join(dir, ".quern", "knowledge", "screens", "login.md"), "---\n");
    writeFileSync(join(dir, ".quern", "knowledge", "screens", "map.md"), "---\n");
    writeFileSync(join(dir, ".quern", "knowledge", "screens", "_template.md"), "---\n");
  }
  if (bundle !== null) {
    mkdirSync(join(dir, ".quern"), { recursive: true });
    writeFileSync(join(dir, ".quern", "config.json"), JSON.stringify({ bundle_id: bundle }));
  }
  return dir;
}

/** A fake server: answers GET /api/v1/landmarks/ with `listing` and the
 * load with `load`, recording every call. */
function fakeRequest({ listing = { sets: {}, sources: {} }, load = { screens: 2 } } = {}) {
  const calls = [];
  const request = async (...args) => {
    calls.push(args);
    if (args[0] === "GET") return listing;
    if (load instanceof Error) throw load;
    return load;
  };
  return { calls, request };
}

test("a project with a knowledge base is found, counted and named", () => {
  const dir = project({ knowledge: true, bundle: APP });
  const pk = detectProjectKnowledge(dir);
  assert.equal(pk.knowledgeDir, join(dir, ".quern", "knowledge"));
  assert.equal(pk.screenFiles, 2, "templates are not screens");
  assert.equal(pk.bundleId, APP);
});

test("a knowledge directory with no screens/ is not a knowledge base", () => {
  const dir = project({ bundle: APP, files: { ".quern/knowledge/README.md": "# kb" } });
  assert.equal(detectProjectKnowledge(dir).knowledgeDir, null);
});

test("a bundle id is a non-empty string or nothing", () => {
  for (const config of ["{not json", JSON.stringify({ bundle_id: "" }),
    JSON.stringify({ bundle_id: null }), JSON.stringify({ bundle_id: 7 })]) {
    const dir = project({ knowledge: true, files: { ".quern/config.json": config } });
    assert.equal(detectProjectKnowledge(dir).bundleId, null, config);
  }
});

test("app projects are recognised by their native project files", () => {
  const cases = [
    [{ "MyApp.xcodeproj": "dir" }, /Xcode project \(MyApp\.xcodeproj\)/],
    [{ "App.xcworkspace": "dir" }, /Xcode project/],
    [{ "settings.gradle.kts": "", "app/src/main/AndroidManifest.xml": MANIFEST },
      /Android project \(settings\.gradle\.kts\)/],
    [{ "settings.gradle": "", "app/src/main/AndroidManifest.xml": MANIFEST },
      /Android project \(settings\.gradle\)/],
    [{ "pubspec.yaml": "", "ios": "dir" }, /Flutter project/],
    [{ "pubspec.yaml": "", "android": "dir" }, /Flutter project/],
    [{ "ios/App.xcodeproj": "dir" }, /iOS project \(ios\/App\.xcodeproj\)/],
    [{ "android/settings.gradle": "", "android/app/src/main/AndroidManifest.xml": MANIFEST },
      /Android project \(android\/settings\.gradle\)/],
    [{ "android/settings.gradle.kts": "", "android/app/src/main/AndroidManifest.xml": MANIFEST },
      /Android project \(android\/settings\.gradle\.kts\)/],
  ];
  for (const [files, marker] of cases) {
    assert.match(appProjectMarker(project({ files })) ?? "", marker, JSON.stringify(files));
  }
  for (const files of [
    { "pubspec.yaml": "" },
    { "settings.gradle.kts": "", "service/src/main/kotlin/App.kt": "" },
    { "android/settings.gradle": "" },
  ]) {
    assert.equal(appProjectMarker(project({ files })), null,
      `${JSON.stringify(files)}: a library or a JVM service is not an app project`);
  }
});

test("QUERN_PROJECT_DIR takes ~ and relative paths", () => {
  assert.equal(projectDirFromEnv(undefined, "/work"), "/work");
  assert.equal(projectDirFromEnv("", "/work"), "/work");
  assert.equal(projectDirFromEnv("~", "/work"), homedir());
  assert.equal(projectDirFromEnv("~/src/app", "/work"), join(homedir(), "src", "app"));
  assert.equal(projectDirFromEnv("../app", "/work/quern"), "/work/app");
  assert.equal(projectDirFromEnv("/abs/app", "/work"), "/abs/app");
});

test("autoload sends the knowledge directory and the app, bounded", async () => {
  const dir = project({ knowledge: true, bundle: APP });
  const pk = detectProjectKnowledge(dir);
  const { calls, request } = fakeRequest();
  const outcome = await autoloadKnowledge(pk, request, true);
  assert.deepEqual(outcome, { kind: "loaded", app: APP, screens: 2 });
  assert.deepEqual(calls.map((c) => c.slice(0, 2)),
    [["GET", "/api/v1/landmarks/"], ["POST", "/api/v1/landmarks/load"]]);
  assert.deepEqual(calls[1][3], { source: join(dir, ".quern", "knowledge"), app: APP });
  assert.equal(AUTOLOAD_TIMEOUT_MS, 5000);
  for (const call of calls) assert.equal(call[4], AUTOLOAD_TIMEOUT_MS, "every request is bounded");
});

test("a load of nothing is not a load", async () => {
  const pk = detectProjectKnowledge(project({ knowledge: true, bundle: APP }));
  for (const load of [{ screens: 0 }, { detail: "not a directory" }, null]) {
    const outcome = await autoloadKnowledge(pk, fakeRequest({ load }).request, true);
    assert.equal(outcome.kind, "not_loaded", JSON.stringify(load));
  }
  const refused = await autoloadKnowledge(
    pk, fakeRequest({ load: new Error("HTTP 400: not a directory") }).request, true);
  assert.equal(refused.kind, "not_loaded", "a status is the server's answer");
});

test("a load sent and never answered is unknown, not absent", async () => {
  // The server does not cancel the scan when the client gives up, so the set
  // can arrive after start-up said it was missing (CodeRabbit on #391).
  const pk = detectProjectKnowledge(project({ knowledge: true, bundle: APP }));
  const timedOut = await autoloadKnowledge(
    pk, fakeRequest({ load: new Error("The operation was aborted due to timeout") }).request, true);
  assert.equal(timedOut.kind, "load_unknown");
  const text = knowledgeInstructions(pk, timedOut).join("\n");
  assert.match(text, /may still finish/);
  assert.match(text, /list_landmarks/);
  assert.doesNotMatch(text, /NOT loaded/);

  // A load whose connection was refused never left: that is an answer.
  const refusedConn = Object.assign(new TypeError("fetch failed"),
    { cause: Object.assign(new Error("connect ECONNREFUSED"), { code: "ECONNREFUSED" }) });
  const neverSent = await autoloadKnowledge(pk, fakeRequest({ load: refusedConn }).request, true);
  assert.equal(neverSent.kind, "not_loaded", "nothing was delivered, so nothing can finish");
  assert.match(neverSent.reason, /ECONNREFUSED/);

  // A reset may come after the server has the request.
  const reset = Object.assign(new TypeError("fetch failed"),
    { cause: Object.assign(new Error("socket hang up"), { code: "ECONNRESET" }) });
  assert.equal(
    (await autoloadKnowledge(pk, fakeRequest({ load: reset }).request, true)).kind, "load_unknown");

  // Failing before the load was sent is still a plain "not loaded".
  const listingDown = await autoloadKnowledge(pk, async () => { throw new Error("ECONNREFUSED"); }, true);
  assert.deepEqual(listingDown, { kind: "not_loaded", reason: "ECONNREFUSED" });
});

test("a set already loaded is kept: this checkout's said as such, another's left in place", async () => {
  const dir = project({ knowledge: true, bundle: APP });
  const pk = detectProjectKnowledge(dir);
  const mine = fakeRequest({ listing: { sets: { [APP]: 9 }, sources: { [APP]: pk.knowledgeDir } } });
  assert.deepEqual(await autoloadKnowledge(pk, mine.request, true),
    { kind: "already", app: APP, screens: 9 });
  assert.equal(mine.calls.length, 1, "nothing loaded over it");

  const theirs = fakeRequest({ listing: { sets: { [APP]: 9 }, sources: { [APP]: "/elsewhere/kb" } } });
  assert.deepEqual(await autoloadKnowledge(pk, theirs.request, true),
    { kind: "other", app: APP, from: "/elsewhere/kb" });
  assert.equal(theirs.calls.length, 1, "another session's set is not replaced");

  // The same checkout through a symlink is still this one.
  const link = join(mkdtempSync(join(tmpdir(), "quern-pk-link-")), "kb");
  symlinkSync(pk.knowledgeDir, link);
  const linked = fakeRequest({ listing: { sets: { [APP]: 9 }, sources: { [APP]: link } } });
  assert.equal((await autoloadKnowledge(pk, linked.request, true)).kind, "already");

  // A server that does not report sources cannot vouch for the set either.
  const old = fakeRequest({ listing: { sets: { [APP]: 9 } } });
  assert.equal((await autoloadKnowledge(pk, old.request, true)).kind, "other");
  assert.equal(old.calls.length, 1);

  // An empty set protects nothing, so it is loaded over.
  const empty = fakeRequest({ listing: { sets: { [APP]: 0 }, sources: { [APP]: "/elsewhere/kb" } } });
  assert.equal((await autoloadKnowledge(pk, empty.request, true)).kind, "loaded");
});

test("autoload asks nothing it cannot use", async () => {
  const cases = [
    [project(), false, true, { kind: "none" }],
    [project({ knowledge: true, bundle: APP, files: {} }), "empty", true, { kind: "empty" }],
    [project({ knowledge: true }), false, true,
      { kind: "not_loaded", reason: "its .quern/config.json names no bundle_id" }],
    [project({ knowledge: true, bundle: APP }), false, false,
      { kind: "not_loaded", reason: "the quern server was not reachable" }],
  ];
  for (const [dir, emptyIt, reachable, expected] of cases) {
    const pk = detectProjectKnowledge(dir);
    if (emptyIt) pk.screenFiles = 0;
    const { calls, request } = fakeRequest();
    assert.deepEqual(await autoloadKnowledge(pk, request, reachable), expected);
    assert.equal(calls.length, 0, JSON.stringify(expected));
  }
});

test("the instructions say what happened, never what was hoped", () => {
  const dir = project({ knowledge: true, bundle: APP });
  const pk = detectProjectKnowledge(dir);
  const say = (outcome) => knowledgeInstructions(pk, outcome).join("\n");
  assert.match(say({ kind: "loaded", app: APP, screens: 2 }), /loaded 2 screens for com\.example\.app/);
  assert.match(say({ kind: "already", app: APP, screens: 9 }), /already had its 9 screens/);
  const other = say({ kind: "other", app: APP, from: "/elsewhere/kb" });
  assert.match(other, /loaded from \/elsewhere\/kb, another checkout, and they were left in place/);
  const notLoaded = say({ kind: "not_loaded", reason: "the quern server was not reachable" });
  assert.match(notLoaded, /NOT loaded: the quern server was not reachable/);
  assert.match(notLoaded, new RegExp(`load_landmarks\\(path="${dir.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"\\)`));
  assert.doesNotMatch(notLoaded, /loaded \d+ screens/);
  assert.match(say({ kind: "empty" }), /no screens yet/);
});

test("with no bundle id the recovery call names the app", () => {
  const pk = detectProjectKnowledge(project({ knowledge: true }));
  const text = knowledgeInstructions(pk, { kind: "not_loaded", reason: "x" }).join("\n");
  assert.match(text, /app="<bundle id>"/);
});

test("an app project with no knowledge base is told to suggest one, not build it", () => {
  const pk = detectProjectKnowledge(project({ files: { "MyApp.xcodeproj": "dir" } }));
  const text = knowledgeInstructions(pk, { kind: "none" }).join("\n");
  assert.match(text, /none in this project/);
  assert.match(text, /Xcode project \(MyApp\.xcodeproj\)/);
  assert.match(text, /suggest building one to the user -- do not start it unasked/);
  assert.match(text, /init_app_knowledge/);
  assert.match(text, /list_landmarks/, "a knowledge base kept elsewhere may be loaded");
});

test("a directory that is not an app project gets nothing", () => {
  const pk = detectProjectKnowledge(project({ files: { "README.md": "# docs", "server/main.py": "" } }));
  assert.deepEqual(knowledgeInstructions(pk, { kind: "none" }), []);
});

// --- The wiring: the built server, started in a project, says what it did ---

const INDEX = join(dirname(fileURLToPath(import.meta.url)), "..", "dist", "index.js");

/** Start dist/index.js in `cwd` against `serverUrl`, send initialize, and
 * return the instructions it answers with. HOME is a temporary directory, so
 * nothing reads the real ~/.quern. */
const HOST_ID = "e".repeat(32);

/** `hostId`: this machine's host-id, as a server here would have written it.
 * The fake servers below answer HOST_ID, so the default shares their disk. */
function handshake(cwd, serverUrl, { hostId = HOST_ID } = {}) {
  return new Promise((resolve, reject) => {
    const home = mkdtempSync(join(tmpdir(), "quern-pk-home-"));
    mkdirSync(join(home, ".quern"));
    writeFileSync(join(home, ".quern", "host-id"), hostId);
    const child = spawn(process.execPath, [INDEX], {
      cwd,
      env: { PATH: process.env.PATH, HOME: home, QUERN_SERVER_URL: serverUrl },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let out = "";
    let err = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`no initialize reply in 20s; stderr: ${err}`));
    }, 20000);
    child.stderr.on("data", (d) => { err += d; });
    child.stdout.on("data", (d) => {
      out += d;
      const line = out.split("\n").find((l) => l.includes('"id":1'));
      if (line) {
        clearTimeout(timer);
        child.kill();
        resolve(JSON.parse(line).result.instructions);
      }
    });
    child.on("error", reject);
    child.stdin.write(JSON.stringify({
      jsonrpc: "2.0", id: 1, method: "initialize",
      params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "t", version: "1" } },
    }) + "\n");
  });
}

test("started in a project with no server, it says the load did not happen", async () => {
  const dir = project({ knowledge: true, bundle: APP });
  // A port nothing listens on: bind one, then close it.
  const probe = createServer();
  await new Promise((r) => probe.listen(0, "127.0.0.1", r));
  const { port } = probe.address();
  await new Promise((r) => probe.close(r));
  const instructions = await handshake(dir, `http://127.0.0.1:${port}`);
  assert.match(instructions, /NOT loaded: the quern server was not reachable/);
  assert.doesNotMatch(instructions, /loaded \d+ screens/);
});

function knowledgeServer(seen) {
  const fake = createServer((req, res) => {
    let body = "";
    req.on("data", (d) => { body += d; });
    req.on("end", () => {
      seen.push([req.method, req.url, body]);
      res.setHeader("content-type", "application/json");
      if (req.url === "/health") res.end(JSON.stringify({ status: "ok", host_id: HOST_ID }));
      else if (req.method === "GET") res.end(JSON.stringify({ sets: {}, sources: {} }));
      else if (req.url === "/api/v1/sessions") {
        res.end(JSON.stringify({ id: "s", resume_token: "TOK" }));
      } else if (req.url.startsWith("/api/v1/uploads")) {
        res.end(JSON.stringify({ upload_id: "up_9", kind: "knowledge" }));
      } else res.end(JSON.stringify({ loaded: APP, screens: 2 }));
    });
  });
  return new Promise((r) => fake.listen(0, "127.0.0.1", () => r(fake)));
}

test("started on another machine's server, it uploads the knowledge base, never its path", async () => {
  // A server on another disk cannot read this path (#254, 3b); the knowledge
  // base is sent instead and loaded for this session alone (#560).
  const dir = project({ knowledge: true, bundle: APP });
  const seen = [];
  const fake = await knowledgeServer(seen);
  try {
    const instructions = await handshake(dir, `http://127.0.0.1:${fake.address().port}`,
                                         { hostId: "f".repeat(32) });
    assert.match(instructions, /loaded for this session only: 2 screens for com\.example\.app/);
    const opened = seen.findIndex(([m, url]) => m === "POST" && url === "/api/v1/sessions");
    const sent = seen.findIndex(([m, url]) => m === "POST" && url.startsWith("/api/v1/uploads"));
    const loaded = seen.findIndex(([m, url]) => m === "POST" && url === "/api/v1/landmarks/load");
    assert.ok(opened >= 0 && opened < sent && sent < loaded, JSON.stringify(seen.map((x) => x[1])));
    assert.match(seen[sent][1], /name=com\.example\.app\.knowledge\.tar\.gz/);
    assert.deepEqual(JSON.parse(seen[loaded][2]), { upload_id: "up_9", app: APP });
    assert.ok(!seen.some(([, , body]) => body.includes(dir)), "this machine's path was sent");
  } finally {
    fake.close();
  }
});

test("started in a project with a server, it loads and says so", async () => {
  const dir = project({ knowledge: true, bundle: APP });
  const seen = [];
  const fake = await knowledgeServer(seen);
  try {
    const instructions = await handshake(dir, `http://127.0.0.1:${fake.address().port}`);
    assert.match(instructions, /loaded 2 screens for com\.example\.app at the start of this session/);
    const load = seen.find(([m, url]) => m === "POST" && url === "/api/v1/landmarks/load");
    assert.ok(load, JSON.stringify(seen));
    assert.deepEqual(JSON.parse(load[2]), { source: join(dir, ".quern", "knowledge"), app: APP });
  } finally {
    fake.close();
  }
});

test("autoload skipped for a reason sends nothing and says why, not load_landmarks with a path", async () => {
  // A server on another disk cannot read this path, and a client key may not
  // load the server-wide sets (#254, phase 3b).
  const dir = project({ knowledge: true, bundle: APP });
  const pk = detectProjectKnowledge(dir);
  const { calls, request } = fakeRequest();
  const outcome = await autoloadKnowledge(pk, request, true, "the quern server is another machine");
  assert.deepEqual(outcome, { kind: "skipped", reason: "the quern server is another machine" });
  assert.equal(calls.length, 0, "nothing was sent");
  const lines = knowledgeInstructions(pk, outcome).join("\n");
  assert.match(lines, /not loaded: the quern server is another machine/);
  assert.doesNotMatch(lines, /load_landmarks\(path=/, "a local path would mean nothing there");
});


// -- uploaded for this session (#560) -----------------------------------------

function sessionLoad({ opens = true, upload = async () => ({ upload_id: "up_1" }),
                       answer = { screens: 3 } } = {}) {
  const calls = [];
  const request = async (method, path, params, body) => {
    calls.push({ method, path, body });
    if (answer instanceof Error) throw answer;
    return answer;
  };
  return { calls, args: [async () => opens, upload, request] };
}

test("a client's knowledge base is uploaded and loaded for its session", async () => {
  const dir = project({ files: { ".quern/knowledge/screens/home.md": "x",
                                 ".quern/config.json": JSON.stringify({ bundle_id: APP }) } });
  const pk = detectProjectKnowledge(dir);
  const { calls, args } = sessionLoad();
  const outcome = await uploadKnowledgeForSession(pk, true, ...args);
  assert.deepEqual(outcome, { kind: "session_loaded", app: APP, screens: 3 });
  assert.deepEqual(calls, [{ method: "POST", path: "/api/v1/landmarks/load",
                             body: { upload_id: "up_1", app: APP } }]);
  const lines = knowledgeInstructions(pk, outcome).join(" ");
  assert.match(lines, /loaded for this session only/);
  assert.match(lines, /other sessions are unaffected/);
});

test("a session that cannot be opened loads nothing, and says so", async () => {
  const dir = project({ files: { ".quern/knowledge/screens/home.md": "x",
                                 ".quern/config.json": JSON.stringify({ bundle_id: APP }) } });
  let uploaded = false;
  const { calls, args } = sessionLoad({ opens: false,
                                        upload: async () => { uploaded = true; return {}; } });
  const outcome = await uploadKnowledgeForSession(detectProjectKnowledge(dir), true, ...args);
  assert.equal(outcome.kind, "not_loaded");
  assert.equal(uploaded, false);
  assert.deepEqual(calls, []);
});

test("a failed upload or a refused load is not_loaded, never thrown", async () => {
  const dir = project({ files: { ".quern/knowledge/screens/home.md": "x",
                                 ".quern/config.json": JSON.stringify({ bundle_id: APP }) } });
  const pk = detectProjectKnowledge(dir);
  const failing = sessionLoad({ upload: async () => { throw new Error("HTTP 413: too big"); } });
  assert.deepEqual(await uploadKnowledgeForSession(pk, true, ...failing.args),
                   { kind: "not_loaded", reason: "HTTP 413: too big" });
  const refused = sessionLoad({ answer: new Error("HTTP 409: no held session") });
  assert.equal((await uploadKnowledgeForSession(pk, true, ...refused.args)).kind, "not_loaded");
});

test("nothing to upload, or no bundle id, sends nothing", async () => {
  const { calls, args } = sessionLoad();
  const none = detectProjectKnowledge(project());
  assert.equal((await uploadKnowledgeForSession(none, true, ...args)).kind, "none");
  const unnamed = detectProjectKnowledge(project({
    files: { ".quern/knowledge/screens/home.md": "x" } }));
  assert.equal((await uploadKnowledgeForSession(unnamed, true, ...args)).kind, "not_loaded");
  assert.deepEqual(calls, []);
});


test("a load_landmarks path is read as the server reads one (#560)", () => {
  const dir = project({ files: { ".quern/knowledge/screens/home.md": "x",
                                 ".quern/config.json": JSON.stringify({ bundle_id: APP }) } });
  const kb = join(dir, ".quern", "knowledge");
  for (const given of [dir, join(dir, ".quern"), kb]) {
    assert.deepEqual(knowledgeAt(given), { dir: kb, bundleId: APP }, given);
  }
  assert.equal(knowledgeAt(project()), null);
  assert.deepEqual(knowledgeAt(join(kb, "screens")), { dir: kb, bundleId: APP });
  const bare = project({ files: { "kb/screens/home.md": "x" } });
  assert.deepEqual(knowledgeAt(join(bare, "kb")), { dir: join(bare, "kb"), bundleId: null });
});


test("a session opened afresh gets its knowledge base again, or is told it did not", async () => {
  const dir = project({ files: { ".quern/knowledge/screens/home.md": "x",
                                 ".quern/config.json": JSON.stringify({ bundle_id: APP }) } });
  const pk = detectProjectKnowledge(dir);
  const told = [];
  const ok = sessionLoad();
  const again = await reloadForNewSession(pk, ok.args[1], ok.args[2], (t) => told.push(t));
  assert.equal(again.kind, "session_loaded");
  assert.deepEqual(told, []);
  const failed = sessionLoad({ answer: new Error("HTTP 413: too big") });
  await reloadForNewSession(pk, failed.args[1], failed.args[2], (t) => told.push(t));
  assert.equal(told.length, 1);
  assert.match(told[0], /lost with the session and could not be loaded again \(HTTP 413/);
});

import { detectProjectPlatform, projectPlatforms } from "../dist/project-knowledge.js";
import { platformFields } from "../dist/platform.js";

test("a project's platform: the knowledge base's word, else its files (#582)", () => {
  const ios = project({ files: { "App.xcodeproj": "dir" } });
  assert.deepEqual(detectProjectPlatform(ios), { platform: "ios", source: "project" });

  const android = project({ files: { "settings.gradle.kts": "", "app/src/main/AndroidManifest.xml": MANIFEST } });
  assert.deepEqual(detectProjectPlatform(android), { platform: "android", source: "project" });

  const both = project({ files: { "pubspec.yaml": "", "ios/Runner.xcodeproj": "dir",
    "android/settings.gradle": "", "android/app/src/main/AndroidManifest.xml": MANIFEST } });
  assert.deepEqual([...projectPlatforms(both)].sort(), ["android", "ios"]);
  assert.equal(detectProjectPlatform(both), null, "cross-platform: the request or the matches decide");

  const backend = project({ files: { "settings.gradle": "" } });
  assert.equal(detectProjectPlatform(backend), null, "a JVM build with no Android module says nothing");

  mkdirSync(join(both, ".quern"), { recursive: true });
  mkdirSync(join(ios, ".quern"), { recursive: true });
  writeFileSync(join(both, ".quern", "config.json"), JSON.stringify({ platform: "android" }));
  assert.deepEqual(detectProjectPlatform(both), { platform: "android", source: "knowledge_base" });
  writeFileSync(join(ios, ".quern", "config.json"), JSON.stringify({ platform: ["ios", "android"] }));
  assert.equal(detectProjectPlatform(ios), null, "both named: deliberately neither, the files not asked");
  writeFileSync(join(ios, ".quern", "config.json"), JSON.stringify({ platform: null }));
  assert.deepEqual(detectProjectPlatform(ios), { platform: "ios", source: "project" },
                   "unset falls through to the files");
});

test("a request's platform: the agent's restricts, the project's is a default (#582)", () => {
  const project = { platform: "ios", source: "knowledge_base" };
  assert.deepEqual(platformFields("android", project), { platform: "android" });
  assert.deepEqual(platformFields(undefined, project),
                   { platform: "ios", platform_source: "knowledge_base" });
  assert.deepEqual(platformFields(undefined, null), {});
});

import { registerAppKnowledgeTools } from "../dist/tools/app-knowledge.js";
import { readFileSync } from "node:fs";

test("init_app_knowledge writes the project's platform into a new config (#582)", async () => {
  const tools = {};
  registerAppKnowledgeTools({ registerTool: (name, _def, handler) => { tools[name] = handler; } });
  const ios = project({ files: { "App.xcodeproj": "dir" } });
  const r = await tools.init_app_knowledge({ project_dir: ios });
  assert.match(r.content[0].text, /platform: ios \(read from the project/);
  assert.equal(JSON.parse(readFileSync(join(ios, ".quern", "config.json"), "utf-8")).platform, "ios");

  const both = project({ files: { "ios/Runner.xcodeproj": "dir", "android/settings.gradle": "",
    "android/app/src/main/AndroidManifest.xml": MANIFEST } });
  const r2 = await tools.init_app_knowledge({ project_dir: both });
  assert.match(r2.content[0].text, /builds for iOS and Android/);
  assert.equal(JSON.parse(readFileSync(join(both, ".quern", "config.json"), "utf-8")).platform, null);
});
