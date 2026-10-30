// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { quernRemoteList } from "../dist/tools/system.js";

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
