// Paths when the server's disk is not this one (#254, phase 3b): the tar a
// .app bundle is sent as, the upload itself, and the shared-disk check.

import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import {
  chmodSync, mkdirSync, mkdtempSync, readFileSync, readlinkSync, rmSync, statSync, symlinkSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Readable } from "node:stream";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { gunzipSync } from "node:zlib";

import { isClientKey, tarTree, uploadApp } from "../dist/remote-paths.js";

let dir;
let app;
const LONG = "d".repeat(60) + "/" + "f".repeat(70);     // past ustar's 100 bytes

before(() => {
  dir = mkdtempSync(join(tmpdir(), "quern-paths-"));
  app = join(dir, "My.app");
  mkdirSync(join(app, "Frameworks/K.framework/Versions/A"), { recursive: true });
  writeFileSync(join(app, "My"), "binary");
  chmodSync(join(app, "My"), 0o755);
  writeFileSync(join(app, "Info.plist"), "<plist/>");
  writeFileSync(join(app, "empty"), "");
  writeFileSync(join(app, "Frameworks/K.framework/Versions/A/K"), "lib");
  symlinkSync("A", join(app, "Frameworks/K.framework/Versions/Current"));
  symlinkSync("Versions/Current/K", join(app, "Frameworks/K.framework/K"));
  mkdirSync(join(app, LONG.split("/")[0]));
  writeFileSync(join(app, LONG), "long");
  writeFileSync(join(app, "résumé.txt"), "unicode");
});

after(() => rmSync(dir, { recursive: true, force: true }));

async function collect(iter) {
  const parts = [];
  for await (const p of iter) parts.push(p);
  return Buffer.concat(parts);
}

test("a bundle's tar unpacks with the system tar exactly: symlinks, modes, long and unicode names", async () => {
  const tar = await collect(tarTree(app, "My.app"));
  assert.equal(tar.length % 512, 0);
  const out = mkdtempSync(join(dir, "x-"));
  writeFileSync(join(out, "a.tar"), tar);
  execFileSync("tar", ["-xf", join(out, "a.tar"), "-C", out]);
  const got = join(out, "My.app");
  assert.equal(readFileSync(join(got, "My"), "utf-8"), "binary");
  assert.equal(statSync(join(got, "My")).mode & 0o111, 0o111, "the executable stays executable");
  assert.equal(readFileSync(join(got, "empty"), "utf-8"), "");
  assert.equal(readlinkSync(join(got, "Frameworks/K.framework/Versions/Current")), "A");
  assert.equal(readFileSync(join(got, "Frameworks/K.framework/K"), "utf-8"), "lib");
  assert.equal(readFileSync(join(got, LONG), "utf-8"), "long");
  assert.equal(readFileSync(join(got, "résumé.txt"), "utf-8"), "unicode");
});

test("a client key always uploads; the owner's key does not by that alone", () => {
  assert.equal(isClientKey("qc_abc"), true);
  assert.equal(isClientKey("k".repeat(32)), false);
});

function recorder(reply) {
  const seen = [];
  const server = createServer(async (req, res) => {
    const body = await collect(req);
    seen.push({ method: req.method, url: req.url, headers: req.headers, body });
    const [status, json] = reply(req);
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(json));
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () =>
    resolve({ server, seen, url: `http://127.0.0.1:${server.address().port}` })));
}

test("a .app is sent as name.app.tar.gz, gzipped, attributed, and its tar holds the bundle", async () => {
  const r = await recorder(() => [200, { upload_id: "up_1", name: "My.app.tar.gz", kind: "app",
                                         bytes: 1, expires_in: 3600 }]);
  try {
    const up = await uploadApp({ url: r.url, apiKey: "qc_k" }, app + "/");
    assert.equal(up.upload_id, "up_1");
    const [req] = r.seen;
    assert.equal(req.method, "POST");
    assert.equal(req.url, "/api/v1/uploads?name=My.app.tar.gz");
    assert.equal(req.headers.authorization, "Bearer qc_k");
    assert.ok(req.headers["x-quern-session"], "attributed to the session");
    const out = mkdtempSync(join(dir, "u-"));
    writeFileSync(join(out, "b.tar"), gunzipSync(req.body));
    execFileSync("tar", ["-xf", join(out, "b.tar"), "-C", out]);
    assert.equal(readFileSync(join(out, "My.app/My"), "utf-8"), "binary");
  } finally {
    r.server.close();
  }
});

test("an .ipa is sent as it is, with its length", async () => {
  const ipa = join(dir, "My.ipa");
  writeFileSync(ipa, "PK-ipa-bytes");
  const r = await recorder(() => [200, { upload_id: "up_2" }]);
  try {
    await uploadApp({ url: r.url, apiKey: "qc_k" }, ipa);
    const [req] = r.seen;
    assert.equal(req.url, "/api/v1/uploads?name=My.ipa");
    assert.equal(req.headers["content-length"], "12");
    assert.equal(req.body.toString(), "PK-ipa-bytes");
  } finally {
    r.server.close();
  }
});

test("a refusal comes back in the server's words; a missing or odd path says what to give", async () => {
  const r = await recorder(() => [413, { detail: "the upload is over the cap" }]);
  try {
    await assert.rejects(uploadApp({ url: r.url, apiKey: "qc_k" }, app), /HTTP 413: .*over the cap/);
  } finally {
    r.server.close();
  }
  await assert.rejects(uploadApp({ url: "http://127.0.0.1:1", apiKey: "qc_k" }, join(dir, "nope.ipa")),
                       /not on this machine/);
  writeFileSync(join(dir, "notes.txt"), "x");
  await assert.rejects(uploadApp({ url: "http://127.0.0.1:1", apiKey: "qc_k" }, join(dir, "notes.txt")),
                       /not an app/);
});

test("the disk is shared only when /health's host_id is this machine's", async () => {
  const home = mkdtempSync(join(dir, "home-"));
  mkdirSync(join(home, ".quern"));
  writeFileSync(join(home, ".quern", "host-id"), "a".repeat(32) + "\n");
  const ids = ["a".repeat(32), "b".repeat(32), "not-an-id"];
  let i = 0;
  const r = await recorder(() => [200, { status: "ok", host_id: ids[i] }]);
  const mod = fileURLToPath(new URL("../dist/remote-paths.js", import.meta.url));
  const ask = () => new Promise((resolve) => execFile(process.execPath, ["--input-type=module", "-e",
    `const m = await import(${JSON.stringify(mod)});
     console.log(await m.sharesDisk({ url: ${JSON.stringify(r.url)}, apiKey: "k" }),
                 await m.mustUpload({ url: ${JSON.stringify(r.url)}, apiKey: "k" }),
                 await m.mustUpload({ url: ${JSON.stringify(r.url)}, apiKey: "qc_k" }));`],
    { env: { ...process.env, HOME: home } }, (err, stdout, stderr) => resolve(stdout.trim() || stderr)));
  try {
    assert.equal(await ask(), "true false true", "same id: shared, and only a client key uploads");
    i = 1;
    assert.equal(await ask(), "false true true", "another machine's id");
    i = 2;
    assert.equal(await ask(), "false true true", "an id that is not one reads as not shared");
  } finally {
    r.server.close();
  }
});
