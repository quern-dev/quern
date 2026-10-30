// Paths when the server's disk is not this one (#254, phase 3b): the tar a
// .app bundle is sent as, the upload itself, and the shared-disk check.

import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import {
  chmodSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readlinkSync, rmSync, statSync, symlinkSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Readable } from "node:stream";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { gunzipSync } from "node:zlib";

import { isClientKey, knowledgeIncluded, tarTree, uploadApp } from "../dist/remote-paths.js";

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
    let body;
    try {
      body = await collect(req);
    } catch {
      return;           // the client gave up mid-upload: nothing to answer
    }
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

// -- paths in answers --------------------------------------------------------

import { MAX_INLINE, withRemotePaths } from "../dist/remote-paths.js";

function remoteServer() {
  // Another machine's id, so this process's disk is not the server's.
  const seen = [];
  const server = createServer((req, res) => {
    seen.push(req.url);
    if (req.url === "/health") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ status: "ok", host_id: "c".repeat(32) }));
    } else if (req.url.startsWith("/api/v1/screenshots?path=") && !req.url.includes("gone")) {
      res.writeHead(200, { "Content-Type": "image/png" });
      res.end(Buffer.from("PNG:" + decodeURIComponent(req.url.split("=")[1])));
    } else {
      res.writeHead(404, { "Content-Type": "application/json" });
      res.end('{"detail": "no screenshot"}');
    }
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () =>
    resolve({ server, seen, info: { url: `http://127.0.0.1:${server.address().port}`, apiKey: "qc_k" } })));
}

const text = (t) => ({ content: [{ type: "text", text: t }] });

test("a remote answer's screenshots come inline, with a line saying whose paths these are", async () => {
  const r = await remoteServer();
  try {
    const answer = JSON.stringify({ tapped: true, screenshots: {
      before: "/tmp/quern/screenshots/2026_tap_before_aa11bb.png",
      after: "/tmp/quern/screenshots/2026_tap_after_cc22dd.png" } });
    const out = await withRemotePaths("tap_element", text(answer), () => r.info);
    const images = out.content.filter((c) => c.type === "image");
    assert.equal(images.length, 2);
    assert.equal(Buffer.from(images[0].data, "base64").toString(),
                 "PNG:/tmp/quern/screenshots/2026_tap_before_aa11bb.png");
    assert.equal(out.content[0].text, answer, "the answer itself is untouched");
    assert.match(out.content[1].text, /on the quern server's disk/);
    assert.ok(r.seen.some((u) => u.includes("screenshots?path=")), "fetched by path");
  } finally {
    r.server.close();
  }
});

test("an error answer's screenshot comes too; a missing one is named, and the count is capped", async () => {
  const r = await remoteServer();
  try {
    const miss = "HTTP 404: " + JSON.stringify({ detail: { screen_context: {
      screenshot: "/tmp/quern/screenshots/x_tap_not_found_gone.png" } } });
    const out = await withRemotePaths("tap_element", { ...text(miss), isError: true }, () => r.info);
    assert.equal(out.isError, true, "still an error");
    assert.match(out.content.at(-1).text, /Could not fetch: .*gone\.png/);
    const many = JSON.stringify({ entries: Array.from({ length: 9 }, (_, i) => ({
      screenshot: `/tmp/quern/timeline/tl_abc/${String(i).padStart(3, "0")}.png` })) });
    const timeline = await withRemotePaths("stop_screenshot_timeline", text(many), () => r.info);
    assert.equal(timeline.content.filter((c) => c.type === "image").length, MAX_INLINE);
    assert.match(timeline.content[1].text, /3 more screenshot/);
  } finally {
    r.server.close();
  }
});

test("a server path with no screenshot is labelled; this machine's own paths are not", async () => {
  const r = await remoteServer();
  try {
    const crash = text(JSON.stringify({ file_path: "/Users/ci/Library/Logs/x.ips" }));
    const labelled = await withRemotePaths("get_latest_crash", crash, () => r.info);
    assert.equal(labelled.content.length, 2);
    assert.match(labelled.content[1].text, /on the quern server's disk/);
    const saved = text("Screenshot saved to /Users/me/shot.png");
    assert.equal(await withRemotePaths("take_screenshot", saved, () => r.info), saved);
    const before = r.seen.length;
    const plain = text(JSON.stringify({ status: "ok", count: 3 }));
    assert.equal(await withRemotePaths("tap", plain, () => r.info), plain);
    assert.equal(r.seen.length, before, "nothing path-like: the server is not even asked");
  } finally {
    r.server.close();
  }
});

// -- review ------------------------------------------------------------------

test("an unreadable file in a bundle fails the upload, not the MCP process", async () => {
  const bad = join(dir, "Bad.app");
  mkdirSync(bad);
  writeFileSync(join(bad, "locked"), "x");
  chmodSync(join(bad, "locked"), 0o000);
  let uncaught = null;
  const onUncaught = (e) => { uncaught = e; };
  process.on("uncaughtException", onUncaught);
  const r = await recorder(() => [200, { upload_id: "up_x" }]);
  try {
    await assert.rejects(uploadApp({ url: r.url, apiKey: "qc_k" }, bad));
    await new Promise((res) => setTimeout(res, 100));
    assert.equal(uncaught, null, "nothing escaped as an uncaught exception");
  } finally {
    process.off("uncaughtException", onUncaught);
    chmodSync(join(bad, "locked"), 0o644);
    r.server.close();
  }
});

test("a symlinked .app is sent as the bundle it points at", async () => {
  const link = join(dir, "Linked.app");
  symlinkSync(app, link);
  const r = await recorder(() => [200, { upload_id: "up_l" }]);
  try {
    await uploadApp({ url: r.url, apiKey: "qc_k" }, link);
    const out = mkdtempSync(join(dir, "l-"));
    writeFileSync(join(out, "c.tar"), gunzipSync(r.seen[0].body));
    execFileSync("tar", ["-xf", join(out, "c.tar"), "-C", out]);
    assert.equal(r.seen[0].url, "/api/v1/uploads?name=Linked.app.tar.gz");
    // A directory, not a link: extracted here a link would still read
    // through to the original, and the server refuses one anyway.
    assert.ok(lstatSync(join(out, "Linked.app")).isDirectory(), "the bundle itself was sent");
    assert.equal(readFileSync(join(out, "Linked.app/My"), "utf-8"), "binary");
  } finally {
    r.server.close();
  }
});

test("a real 'not shared' answer is kept: /health is asked once, not per result", async () => {
  const r = await remoteServer();
  const mod = fileURLToPath(new URL("../dist/remote-paths.js", import.meta.url));
  const out = await new Promise((resolve) => execFile(process.execPath, ["--input-type=module", "-e",
    `const m = await import(${JSON.stringify(mod)});
     const s = { url: ${JSON.stringify(r.info.url)}, apiKey: "k" };
     for (let i = 0; i < 5; i++) await m.mustUpload(s);
     for (let i = 0; i < 5; i++) await m.withRemotePaths("get_latest_crash",
       { content: [{ type: "text", text: '{"file_path": "/x/y.ips"}' }] }, () => s);
     console.log("done");`], (err, stdout, stderr) => resolve(stdout.trim() || stderr)));
  try {
    assert.equal(out, "done");
    assert.equal(r.seen.filter((u) => u === "/health").length, 1);
  } finally {
    r.server.close();
  }
});


test("a knowledge base's tar holds app.md and the screen files only (#560)", async () => {
  const kb = mkdtempSync(join(dir, "kb-"));
  mkdirSync(join(kb, "screens/nested"), { recursive: true });
  writeFileSync(join(kb, "app.md"), "# app");
  writeFileSync(join(kb, "README.md"), "not sent");
  writeFileSync(join(kb, "screens/home.md"), "home");
  writeFileSync(join(kb, "screens/home.png"), "not yet");
  writeFileSync(join(kb, "screens/nested/deep.md"), "not read by the loader");
  symlinkSync("home.md", join(kb, "screens/link.md"));     // never sent: links are refused
  const tar = await collect(tarTree(kb, "knowledge", knowledgeIncluded));
  const listed = execFileSync("tar", ["-tf", "-"], { input: tar }).toString().trim().split("\n");
  assert.deepEqual(listed.sort(), ["knowledge/", "knowledge/app.md", "knowledge/screens/",
                                   "knowledge/screens/home.md"]);
});
