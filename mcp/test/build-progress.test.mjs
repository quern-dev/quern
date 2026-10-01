// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { progressMessage } from "../dist/tools/build-progress.js";

const build = {
  project: "/src/app-root", task: ":app:assembleStagingDebug", elapsed_s: 134.2,
  current: "> Task :app:compileStagingDebugKotlin", tasks_run: 41,
};

test("names the task Gradle is on and how long it has run", () => {
  assert.equal(progressMessage([build], "/src/app-root", 140),
    ":app:assembleStagingDebug: :app:compileStagingDebugKotlin (2m14s, 41 tasks)");
});

test("a module path inside the build root is the same build", () => {
  const other = { ...build, project: "/src/elsewhere", task: ":x:assembleDebug" };
  assert.match(progressMessage([other, build], "/src/app-root/app/", 1), /assembleStagingDebug/);
});

test("before Gradle starts there is only the wait to report", () => {
  assert.equal(progressMessage([], "/src/app-root", 75), "working (1m15s)");
});

test("one build running and no path match is taken to be ours", () => {
  // The caller's path can differ from the server's by a symlink.
  assert.match(progressMessage([build], "/Volumes/src/app-root", 3), /compileStagingDebugKotlin/);
});

test("two builds and no match is not guessed", () => {
  const a = { ...build, project: "/a" }, b = { ...build, project: "/b" };
  assert.equal(progressMessage([a, b], "/c", 5), "working (5s)");
});

test("a build with no task yet says Gradle is starting", () => {
  assert.match(progressMessage([{ ...build, current: "", tasks_run: 0 }], "/src/app-root", 2),
    /starting Gradle \(2m14s, 0 tasks\)/);
});
