// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";
import { setTimeout as sleep } from "node:timers/promises";

import { progressMessage, reportProgress } from "../dist/tools/build-progress.js";

const build = {
  project: "/src/app-root", task: ":app:assembleStagingDebug", elapsed_s: 134.2,
  current: "> Task :app:compileStagingDebugKotlin", tasks_run: 41, stage: "building",
  progress_id: "mine",
};

test("names the task Gradle is on and how long it has run", () => {
  assert.equal(progressMessage([build], "mine", 140),
    ":app:assembleStagingDebug: :app:compileStagingDebugKotlin (2m14s, 41 tasks)");
});

test("another call's build is never shown as ours", () => {
  // Matched by id, not path: an iOS build, or an Android one still checking
  // its environment, would otherwise show the one Gradle build running.
  const other = { ...build, progress_id: "theirs" };
  assert.equal(progressMessage([other], "mine", 5), "working (5s)");
});

test("an older server that sends no id reports only the wait", () => {
  const { progress_id, ...old } = build;
  assert.equal(progressMessage([old], "mine", 75), "working (1m15s)");
});

test("after Gradle, the install is what is happening", () => {
  // Measured: the last notification of a 130s build read "working (2m10s)"
  // while quern installed, because the build had already left the list.
  assert.equal(progressMessage([{ ...build, stage: "installing on 1 device(s)" }], "mine", 130),
    ":app:assembleStagingDebug: installing on 1 device(s) (2m14s)");
});

test("before the task is known, the stage names what is happening", () => {
  assert.equal(progressMessage([{ ...build, task: "", stage: "checking the variant" }], "mine", 9),
    "Gradle: checking the variant (2m14s)");
});

test("a build with no task line yet says Gradle is starting", () => {
  assert.match(progressMessage([{ ...build, current: "", tasks_run: 0 }], "mine", 2),
    /starting Gradle \(2m14s, 0 tasks\)/);
});

function fakeExtra(token = "tok") {
  const sent = [];
  const controller = new AbortController();
  return {
    sent, controller,
    extra: {
      _meta: token === null ? {} : { progressToken: token },
      signal: controller.signal,
      sendNotification: async (n) => { sent.push(n); },
    },
  };
}

test("no token, no notifications and no polling", async () => {
  const { sent, extra } = fakeExtra(null);
  let polled = 0;
  const stop = reportProgress(extra, "mine", async () => { polled++; return []; }, 5);
  await sleep(40);
  stop();
  assert.equal(sent.length, 0);
  assert.equal(polled, 0);
});

test("notifications carry the token, rising progress and our build's line", async () => {
  const { sent, extra } = fakeExtra();
  const stop = reportProgress(extra, "mine", async () => [build], 5);
  await sleep(60);
  stop();
  assert.ok(sent.length >= 2, `expected several, got ${sent.length}`);
  assert.ok(sent.every((n) => n.params.progressToken === "tok"));
  const steps = sent.map((n) => n.params.progress);
  assert.deepEqual(steps, [...steps].sort((a, b) => a - b));
  assert.match(sent[0].params.message, /compileStagingDebugKotlin/);
});

test("a tick still polling when the tool answers sends nothing after", async () => {
  // A notification after the response is for a token the client has
  // forgotten; the client SDK reports it as an error.
  const { sent, extra } = fakeExtra();
  let release;
  const stop = reportProgress(extra, "mine",
    () => new Promise((resolve) => { release = () => resolve([build]); }), 5);
  await sleep(20);
  stop();
  release();
  await sleep(20);
  assert.equal(sent.length, 0);
});

test("a cancelled request stops the notifications", async () => {
  const { sent, extra, controller } = fakeExtra();
  reportProgress(extra, "mine", async () => [build], 5);
  await sleep(25);
  controller.abort();
  const before = sent.length;
  await sleep(40);
  assert.equal(sent.length, before);
});

test("a failing poll still reports the wait, and never throws", async () => {
  const { sent, extra } = fakeExtra();
  const stop = reportProgress(extra, "mine", async () => { throw new Error("404"); }, 5);
  await sleep(30);
  stop();
  assert.ok(sent.length >= 1);
  assert.match(sent[0].params.message, /^working \(/);
});
