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
  const saved = { PATH: process.env.PATH, HOME: process.env.HOME };
  Object.assign(process.env, env);
  try {
    return await fn();
  } finally {
    Object.assign(process.env, saved);
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
