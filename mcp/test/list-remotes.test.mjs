// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { markCurrent, quernRemoteList } from "../dist/tools/system.js";

function fakeQuern(script) {
  const dir = mkdtempSync(join(tmpdir(), "quern-lr-"));
  const bin = join(dir, "bin");
  mkdirSync(bin);
  writeFileSync(join(bin, "quern"), `#!/bin/sh\n${script}\n`);
  chmodSync(join(bin, "quern"), 0o755);
  return { dir, bin };
}

async function withEnv(env, fn) {
  // QUERN_REMOTE cleared: one exported by a developer would change which
  // server counts as current (CodeRabbit). Unset ones are deleted again, not
  // restored as the string "undefined".
  const keys = [...new Set(["PATH", "HOME", "QUERN_REMOTE", ...Object.keys(env)])];
  const saved = Object.fromEntries(keys.map((k) => [k, process.env[k]]));
  delete process.env.QUERN_REMOTE;
  Object.assign(process.env, env);
  try {
    return await fn();
  } finally {
    for (const k of keys) {
      if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k];
    }
  }
}

test("a remote that is down still gives the list: quern exits 1 and prints it", async () => {
  const { dir, bin } = fakeQuern(
    `echo '{"local_version":"1","remotes":[{"name":"studio","reachable":false,"error":"timed out"}]}'; exit 1`);
  const data = await withEnv({ PATH: `${bin}:/usr/bin:/bin`, HOME: dir }, quernRemoteList);
  assert.equal(data.remotes[0].name, "studio");
  assert.equal(data.remotes[0].error, "timed out");
});

test("output that is not the list is an error naming what quern said", async () => {
  const { dir, bin } = fakeQuern(`echo "usage: quern remote ..." >&2; exit 2`);
  await assert.rejects(withEnv({ PATH: `${bin}:/usr/bin:/bin`, HOME: dir }, quernRemoteList),
                       /quern remote list failed: usage/);
});

test("no quern CLI anywhere says so", async () => {
  const empty = mkdtempSync(join(tmpdir(), "quern-none-"));
  await assert.rejects(withEnv({ PATH: "/usr/bin:/bin", HOME: empty }, quernRemoteList),
                       /quern CLI is not installed/);
});

test("the wrapper in ~/.local/bin is found when quern is not on PATH", async () => {
  const home = mkdtempSync(join(tmpdir(), "quern-home-"));
  mkdirSync(join(home, ".local", "bin"), { recursive: true });
  writeFileSync(join(home, ".local", "bin", "quern"), `#!/bin/sh\necho '{"remotes":[]}'\n`);
  chmodSync(join(home, ".local", "bin", "quern"), 0o755);
  const data = await withEnv({ PATH: "/usr/bin:/bin", HOME: home }, quernRemoteList);
  assert.deepEqual(data, { remotes: [] });
});


test("an older quern on PATH falls back to the installed wrapper", async () => {
  const { dir, bin } = fakeQuern(`echo "usage: quern remote list" >&2; exit 2`);
  mkdirSync(join(dir, ".local", "bin"), { recursive: true });
  writeFileSync(join(dir, ".local", "bin", "quern"), `#!/bin/sh\necho '{"remotes":[{"name":"r"}]}'\n`);
  chmodSync(join(dir, ".local", "bin", "quern"), 0o755);
  const data = await withEnv({ PATH: `${bin}:/usr/bin:/bin`, HOME: dir }, quernRemoteList);
  assert.equal(data.remotes[0].name, "r");
});


test("the server this MCP talks to is marked current, and only it", () => {
  const data = { remotes: [{ name: "local", local: true }, { name: "romulus" }, { name: "studio" }] };
  assert.deepEqual(markCurrent(structuredClone(data), "local").remotes.map((r) => r.current),
                   [true, false, false]);
  assert.deepEqual(markCurrent(structuredClone(data), "romulus").remotes.map((r) => r.current),
                   [false, true, false]);
  assert.equal(markCurrent({ error: "x" }, "local").error, "x");          // not a list: untouched
});

test("it asks for this Mac's server too", async () => {
  const { dir, bin } = fakeQuern(`printf '{"args": "%s"}' "$*"`);
  const data = await withEnv({ PATH: `${bin}:/usr/bin:/bin`, HOME: dir }, quernRemoteList);
  assert.equal(data.args, "remote list --json --with-local");
});

import { matchingElsewhere, elsewhereHint } from "../dist/tools/system.js";
import { currentRemote, selectServer } from "../dist/config.js";

const LIST = {
  remotes: [
    { name: "local", devices: [
      { udid: "L1", name: "iPhone 16 Pro", model: "iPhone 16 Pro", os_version: "iOS 18.6",
        device_family: "iPhone", device_type: "simulator", free: true, state: "booted" },
    ] },
    { name: "romulus", devices: [
      { udid: "R1", name: "iPhone 15 Pro", model: "iPhone 15 Pro", os_version: "iOS 17.5",
        device_family: "iPhone", device_type: "simulator", free: true, state: "booted" },
      { udid: "R2", name: "CI 3", model: "iPhone 15", os_version: "iOS 17.5",
        device_family: "iPhone", device_type: "simulator", free: true, state: "shutdown" },
      { udid: "R3", name: "iPhone SE", model: "iPhone SE", os_version: "iOS 17.5.1",
        device_family: "iPhone", device_type: "simulator", free: false, state: "booted" },
      { udid: "R4", name: "iPad Air", model: "iPad Air", os_version: "iOS 17.5",
        device_family: "iPad", device_type: "simulator", free: true, state: "shutdown" },
      { udid: "R5", name: "iPhone 15", model: "iPhone 15", os_version: "iOS 17.50",
        device_family: "iPhone", device_type: "simulator", free: true, state: "shutdown" },
    ] },
    { name: "studio", reachable: false, devices: null, error: "timed out" },
  ],
};
const udids = (found) => found.flatMap((f) => f.devices.map((d) => `${f.name}:${d.udid}`));

test("elsewhere: the pool's rules -- OS prefix on a dot, family, free only, never the current server", () => {
  assert.deepEqual(udids(matchingElsewhere(LIST, "local", { os_version: "17.5", device_family: "iPhone" })),
                   ["romulus:R1", "romulus:R2"], "17.50 is not 17.5; held R3 is not offered");
  assert.deepEqual(udids(matchingElsewhere(LIST, "local", { os_version: "iOS 17" })),
                   ["romulus:R1", "romulus:R2", "romulus:R4", "romulus:R5"]);
  assert.deepEqual(udids(matchingElsewhere(LIST, "romulus", { device_family: "iphone" })), ["local:L1"],
                   "from a remote, this Mac is elsewhere; family is case-insensitive");
  assert.deepEqual(matchingElsewhere(LIST, "local", { device_type: "device" }), []);
  const unset = { remotes: [{ name: "romulus", devices: [
    { udid: "P1", name: "iPad Pro (M4)", device_family: "", free: true, state: "booted" }] }] };
  assert.deepEqual(udids(matchingElsewhere(unset, "local", { device_family: "iPad" })),
                   ["romulus:P1"], "no family: inferred from the name, as the pool does");
});

test("elsewhere: a name matches name or model, exact before substring", () => {
  assert.deepEqual(udids(matchingElsewhere(LIST, "local", { name: "iphone 15" })), ["romulus:R2", "romulus:R5"],
                   "exact on R2's model and R5's name, so the substring R1 is left out");
  assert.deepEqual(udids(matchingElsewhere(LIST, "local", { name: "15 Pro" })), ["romulus:R1"]);
});

test("the hint names the server and how to switch; nothing when nowhere has a match", async () => {
  const { dir, bin } = fakeQuern(`cat <<'J'\n${JSON.stringify(LIST)}\nJ`);
  const hint = await withEnv({ PATH: `${bin}:/usr/bin:/bin`, HOME: dir },
    () => elsewhereHint({ os_version: "17.5", device_family: "iPhone" }));
  assert.match(hint, /romulus has 2 free that match: iPhone 15 Pro \(booted\), CI 3/);
  assert.match(hint, /use_remote\(name="romulus"\)/);
  const none = await withEnv({ PATH: `${bin}:/usr/bin:/bin`, HOME: dir },
    () => elsewhereHint({ os_version: "16.0" }));
  assert.equal(none, "");
  const broken = fakeQuern("exit 2");
  assert.equal(await withEnv({ PATH: `${broken.bin}:/usr/bin:/bin`, HOME: broken.dir },
    () => elsewhereHint({ os_version: "17.5" })), "", "a list that fails costs only the hint");
});

test("use_remote's selection wins over QUERN_REMOTE, and 'local' means this Mac", () => {
  const saved = process.env.QUERN_REMOTE;
  try {
    process.env.QUERN_REMOTE = "romulus";
    assert.equal(currentRemote(), "romulus");
    selectServer("local");
    assert.equal(currentRemote(), undefined);
    selectServer("studio");
    assert.equal(currentRemote(), "studio");
    selectServer(undefined);
    assert.equal(currentRemote(), "romulus", "undefined goes back to QUERN_REMOTE");
  } finally {
    if (saved === undefined) delete process.env.QUERN_REMOTE; else process.env.QUERN_REMOTE = saved;
  }
});

test("the listing hint is list_devices' own filter: criteria, then name, stored family", () => {
  const data = { remotes: [{ name: "romulus", devices: [
    { udid: "A", name: "iPhone 15", os_version: "iOS 18.6", device_family: "iPhone", free: true, state: "booted" },
    { udid: "B", name: "iPhone 15 Pro", os_version: "iOS 17.5", device_family: "iPhone", free: true, state: "booted" },
    { udid: "C", name: "iPhone SE (2nd generation)", os_version: "iOS 17.5", device_family: "", free: true, state: "booted" },
    { udid: "D", name: "iPhone SE", os_version: "iOS 17.5", device_family: "iPhone", free: false, state: "booted" },
  ] }] };
  // The OS filter runs before the name, as the endpoint now does -- the
  // pool's order, so a list and a reservation agree (#582).
  assert.deepEqual(udids(matchingElsewhere(data, "local", { name: "iPhone 15", os_version: "17.5", listing: true })),
                   ["romulus:B"]);
  assert.deepEqual(udids(matchingElsewhere(data, "local", { name: "iPhone 15", os_version: "17.5" })),
                   ["romulus:B"], "the pool filters first, then names");
  // No family stored: the listing compares it as stored, so it is not an iPhone.
  assert.deepEqual(udids(matchingElsewhere(data, "local", { device_family: "iPhone", os_version: "17.5", listing: true })),
                   ["romulus:B"]);
  // A held exact match still decides what the listing shows: it lists the
  // held one, not the free substring match, so none is offered.
  assert.deepEqual(matchingElsewhere(data, "local", { name: "iPhone SE", listing: true }), []);
});

test("elsewhere: a hint stays on the request's platform and reads 'Android 16' (#582)", () => {
  const list = { remotes: [{ name: "romulus", devices: [
    { udid: "I16", name: "iPhone 14", os_version: "iOS 16.4", device_family: "iPhone",
      device_type: "simulator", free: true, state: "booted" },
    { udid: "A16", name: "Pixel_8_API_36", model: "Pixel 8", os_version: "16",
      device_family: "Android", device_type: "android_emulator", free: true, state: "shutdown" },
  ] }] };
  assert.deepEqual(udids(matchingElsewhere(list, "local", { os_version: "16", platform: "android" })),
                   ["romulus:A16"], "an Android request is not offered an iOS 16 simulator");
  assert.deepEqual(udids(matchingElsewhere(list, "local", { os_version: "Android 16" })),
                   ["romulus:A16"], "a prefixed version names the platform");
  assert.deepEqual(udids(matchingElsewhere(list, "local", { os_version: "iOS 16", listing: true })),
                   ["romulus:I16"]);
  assert.deepEqual(udids(matchingElsewhere(list, "local", { name: "Pixel 8 API 36" })),
                   ["romulus:A16"], "underscores read as spaces");
  assert.deepEqual(udids(matchingElsewhere(list, "local", { name: "pixel 8", listing: true })),
                   ["romulus:A16"], "a listing matches the model too, as the server's list now does");
});

test("elsewhere: the project's platform is crossed as the server crosses it (#582 review)", () => {
  const list = { remotes: [{ name: "romulus", devices: [
    { udid: "P8", name: "Pixel_8_API_36", model: "Pixel 8", os_version: "16",
      device_family: "Android", device_type: "android_emulator", free: true, state: "booted" },
    { udid: "P9", name: "Pixel_9_API_36", model: "Pixel 9", os_version: "16",
      device_family: "Android", device_type: "android_emulator", free: true, state: "booted" },
    { udid: "I18", name: "iPhone 16", os_version: "iOS 18.6", device_family: "iPhone",
      device_type: "simulator", free: true, state: "booted" },
  ] }] };
  const soft = { platform: "ios", platform_source: "project" };
  assert.deepEqual(udids(matchingElsewhere(list, "local", { ...soft, name: "Pixel 8" })),
                   ["romulus:P8"], "one match on the other platform: the server would use it");
  assert.deepEqual(matchingElsewhere(list, "local", { ...soft, os_version: "16" }), [],
                   "two, for one wanted: the server would list, not pick");
  assert.deepEqual(udids(matchingElsewhere(list, "local", { ...soft, os_version: "16", count: 2 })),
                   ["romulus:P8", "romulus:P9"]);
  assert.deepEqual(matchingElsewhere(list, "local", { platform: "ios", name: "Pixel 8" }), [],
                   "the agent's own platform restricts");
  assert.deepEqual(udids(matchingElsewhere(list, "local", { platform: "ios", name: "iPhone", listing: true })),
                   ["romulus:I18"], "a listing keeps to a restricting platform too");
  assert.deepEqual(matchingElsewhere(list, "local", { platform: "android", name: "iPhone", listing: true }), []);
});
