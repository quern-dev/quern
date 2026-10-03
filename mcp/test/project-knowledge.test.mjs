// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  appProjectMarker,
  autoloadKnowledge,
  detectProjectKnowledge,
  knowledgeInstructions,
} from "../dist/project-knowledge.js";

function project({ knowledge = false, bundle = null, files = {} } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "quern-pk-"));
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

test("a project with a knowledge base is found, counted and named", () => {
  const dir = project({ knowledge: true, bundle: "com.example.app" });
  const pk = detectProjectKnowledge(dir);
  assert.equal(pk.knowledgeDir, join(dir, ".quern", "knowledge"));
  assert.equal(pk.screenFiles, 2, "templates are not screens");
  assert.equal(pk.bundleId, "com.example.app");
  const text = knowledgeInstructions(pk).join("\n");
  assert.match(text, /2 screen files for com\.example\.app/);
  assert.match(text, new RegExp(`load_landmarks\\(path="${dir.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"\\)`));
});

test("an app project with no knowledge base is told to suggest one, not build it", () => {
  const dir = project({ files: { "MyApp.xcodeproj": "dir" } });
  const text = knowledgeInstructions(detectProjectKnowledge(dir)).join("\n");
  assert.match(text, /none in this project/);
  assert.match(text, /Xcode project \(MyApp\.xcodeproj\)/);
  assert.match(text, /Suggest building one to the user -- do not start it unasked/);
  assert.match(text, /init_app_knowledge/);
  assert.match(text, /list_landmarks/, "a knowledge base kept elsewhere may be loaded");
});

test("a directory that is not an app project gets nothing", () => {
  const dir = project({ files: { "README.md": "# docs", "server/main.py": "" } });
  assert.deepEqual(knowledgeInstructions(detectProjectKnowledge(dir)), []);
});

test("app projects are recognised by their native project files", () => {
  const cases = [
    [{ "App.xcworkspace": "dir" }, /Xcode project/],
    [{ "settings.gradle.kts": "" }, /Gradle project/],
    [{ "pubspec.yaml": "", "ios": "dir" }, /Flutter project/],
    [{ "ios/App.xcodeproj": "dir" }, /iOS project \(ios\/App\.xcodeproj\)/],
    [{ "android/settings.gradle": "" }, /Android project/],
  ];
  for (const [files, marker] of cases) {
    assert.match(appProjectMarker(project({ files })), marker, JSON.stringify(files));
  }
  assert.equal(appProjectMarker(project({ files: { "pubspec.yaml": "" } })), null,
    "a Dart package with no app targets is not an app project");
});

test("an unreadable project config leaves the app unnamed, never guessed", () => {
  const dir = project({ knowledge: true, files: { ".quern/config.json": "{not json" } });
  const pk = detectProjectKnowledge(dir);
  assert.equal(pk.bundleId, null);
  assert.match(knowledgeInstructions(pk).join("\n"), /no bundle_id/);
});

test("autoload asks the server for the project, and says how it went", async () => {
  const dir = project({ knowledge: true, bundle: "com.example.app" });
  const pk = detectProjectKnowledge(dir);
  const calls = [];
  const ok = await autoloadKnowledge(pk, async (...args) => {
    calls.push(args);
    return { loaded: "com.example.app", screens: 2 };
  });
  assert.deepEqual(calls[0].slice(0, 4),
    ["POST", "/api/v1/landmarks/load", undefined, { source: dir, app: "com.example.app" }]);
  assert.match(ok, /loaded 2 screens for com\.example\.app/);

  const refused = await autoloadKnowledge(pk, async () => ({ detail: "not a directory" }));
  assert.match(refused, /could not load .*not a directory/);
  const down = await autoloadKnowledge(pk, async () => { throw new Error("ECONNREFUSED"); });
  assert.match(down, /could not load .*ECONNREFUSED/);
});

test("autoload asks nothing of a project without a knowledge base", async () => {
  let asked = false;
  const result = await autoloadKnowledge(detectProjectKnowledge(project()), async () => {
    asked = true;
  });
  assert.equal(asked, false);
  assert.match(result, /no knowledge base/);
});
