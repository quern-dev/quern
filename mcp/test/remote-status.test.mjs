// Runs against the compiled output: `npm run build && npm test`.
// The native list_remotes (#605): never the quern CLI, never this Mac's real
// server -- every server here is a fake on loopback, and the local one is
// injected.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { devices, loadTarget, probe, readRemotes, remoteList } from "../dist/remote-status.js";

const here = dirname(fileURLToPath(import.meta.url));
const FIXTURE = JSON.parse(readFileSync(join(here, "..", "..", "tests", "fixtures",
                                             "remote-status.json"), "utf-8"));

for (const c of FIXTURE.cases) {
  test(`shared fixture: ${c.name}`, () => {
    if (c.rows === null) {
      assert.throws(() => devices(c.answer, c.label), /no device list|not an object/);
    } else {
      assert.deepEqual(devices(c.answer, c.label), c.rows);
    }
  });
}

/** A fake quern on loopback: `routes` maps a path to [status, body] or to a
 * function; anything else is 404. Resolves to { url, close, seen }. */
async function fake(routes) {
  const seen = [];
  const server = createServer((req, res) => {
    seen.push({ path: req.url, auth: req.headers.authorization,
                session: req.headers["x-quern-session"] });
    const route = routes[req.url];
    if (typeof route === "function") return route(req, res);
    const [status, body] = route ?? [404, { detail: "nope" }];
    res.writeHead(status, { "content-type": "application/json" });
    res.end(typeof body === "string" ? body : JSON.stringify(body));
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const { port } = server.address();
  return { url: `http://127.0.0.1:${port}`, seen,
           close: () => new Promise((r) => { server.closeAllConnections(); server.close(r); }) };
}

const target = (url, extra = {}) =>
  ({ name: "local", url, key: "k-1", fingerprint: "", label: "this Mac", ...extra });

const OK = { "/health": [200, { status: "ok", version: "0.25.0" }],
             "/api/v1/device/list": [200, { devices: [{ udid: "A", name: "iPhone" }] }] };

test("a server that answers: version, devices, free, and the key and its own session sent", async () => {
  const s = await fake(OK);
  try {
    const row = await probe(target(s.url), "0.25.0");
    assert.equal(row.reachable, true);
    assert.equal(row.version, "0.25.0");
    assert.equal(row.version_matches, true);
    assert.equal(row.free, 1);
    assert.equal(row.devices.length, 1);
    assert.equal(row.error, null);
    assert.ok(s.seen.every((r) => r.auth === "Bearer k-1" && r.session === "quern-remote-status"));
  } finally {
    await s.close();
  }
});

test("another version is said, and no local version leaves it unknown", async () => {
  const s = await fake(OK);
  try {
    assert.equal((await probe(target(s.url), "0.24.0")).version_matches, false);
    assert.equal((await probe(target(s.url), null)).version_matches, null);
  } finally {
    await s.close();
  }
});

test("something that is not quern is said, never read as a server with no devices", async () => {
  const s = await fake({ "/health": [200, "<html>router login</html>"] });
  try {
    const row = await probe(target(s.url), null);
    assert.equal(row.reachable, false);
    assert.equal(row.devices, null);
    assert.match(row.error, /not quern's \(not JSON\)/);
  } finally {
    await s.close();
  }
});

test("a revoked key is 'refused its key'", async () => {
  const s = await fake({ "/health": [401, { detail: "Invalid or missing API key" }] });
  try {
    assert.match((await probe(target(s.url), null)).error, /^refused its key \(HTTP 401\)/);
  } finally {
    await s.close();
  }
});

test("health answered but devices failed: reachable, devices null, and why", async () => {
  const s = await fake({ "/health": OK["/health"], "/api/v1/device/list": [500, { detail: "x" }] });
  try {
    const row = await probe(target(s.url), null);
    assert.equal(row.reachable, true);
    assert.equal(row.devices, null);
    assert.equal(row.free, null);
    assert.equal(row.error, "devices could not be listed: HTTP 500");
  } finally {
    await s.close();
  }
});

test("nothing listening is 'not reachable', and a slow server times out", async () => {
  const s = await fake({});
  const url = s.url;
  await s.close();
  assert.match((await probe(target(url), null)).error, /^not reachable \(ECONNREFUSED\)$/);
  const slow = await fake({ "/health": () => {} });   // never answers
  try {
    const row = await probe(target(slow.url), null, 200);
    assert.equal(row.error, "not reachable (timed out)");
  } finally {
    await slow.close();
  }
});

function remotesFile(content) {
  const dir = mkdtempSync(join(tmpdir(), "quern-rs-"));
  const file = join(dir, "remotes.json");
  if (content !== undefined) {
    writeFileSync(file, typeof content === "string" ? content : JSON.stringify(content));
  }
  return file;
}

const FP = "ab".repeat(32);

test("remotes.json: none is no remotes; unreadable or shapeless is an error, never an empty list", () => {
  assert.deepEqual(readRemotes(remotesFile()), {});
  assert.throws(() => readRemotes(remotesFile("{not json")), /is not readable/);
  assert.throws(() => readRemotes(remotesFile({ remotes: [] })), /no 'remotes' object/);
});

test("an entry that cannot be used says why: http, no key, a bad fingerprint", () => {
  assert.match(loadTarget("a", { url: "http://x", key: "k", fingerprint: FP }), /not an https:\/\//);
  assert.match(loadTarget("a", { url: "https://x", fingerprint: FP }), /incomplete \(no key\)/);
  assert.match(loadTarget("a", { url: "https://x", key: "k", fingerprint: "zz" }),
               /incomplete \(not a SHA-256/);
  const ok = loadTarget("a", { url: "https://x:9130/", key: "k", fingerprint: `SHA256:${FP}`,
                               label: "me" });
  assert.deepEqual(ok, { name: "a", url: "https://x:9130", key: "k", fingerprint: FP, label: "me" });
});

test("the list: this Mac first, remotes by name, a broken entry listed with why", async () => {
  const s = await fake(OK);
  const gone = await fake({});
  const closedPort = new URL(gone.url).port;
  await gone.close();
  try {
    const file = remotesFile({ remotes: {
      zed: { url: "http://plain", key: "k", fingerprint: FP },
      alpha: { url: `https://127.0.0.1:${closedPort}`, key: "k", fingerprint: FP },
    } });
    const out = await remoteList({ localVersion: "0.25.0", file, timeoutMs: 1000,
                                   localServer: () => target(s.url) });
    assert.deepEqual(out.remotes.map((r) => r.name), ["local", "alpha", "zed"]);
    assert.equal(out.remotes[0].local, true);
    assert.equal(out.remotes[0].reachable, true);
    assert.equal(out.remotes[1].local, false);
    assert.equal(out.remotes[1].error, "not reachable (ECONNREFUSED)");
    assert.match(out.remotes[2].error, /not an https:\/\//);
    assert.equal(out.local_version, "0.25.0");
  } finally {
    await s.close();
  }
});

test("a state.json left by a server that is gone reads 'not running here'", async () => {
  const s = await fake({});
  const url = s.url;
  await s.close();
  const out = await remoteList({ file: remotesFile(), localServer: () => target(url) });
  assert.equal(out.remotes[0].error, "not running here (quern start)");
  assert.equal(out.remotes[0].local, true);
});

test("an unreadable remotes.json is one row saying so", async () => {
  const out = await remoteList({ file: remotesFile("{oops"), localServer: () => ({}) });
  assert.equal(out.remotes.length, 1);
  assert.equal(out.remotes[0].name, null);
  assert.match(out.remotes[0].error, /is not readable/);
});

test("the whole list stops at its deadline and names who had not answered", async () => {
  const slow = await fake({ "/health": () => {} });
  try {
    const started = Date.now();
    const out = await remoteList({ file: remotesFile(), timeoutMs: 5000, deadlineMs: 300,
                                   localServer: () => target(slow.url) });
    assert.ok(Date.now() - started < 2000, "waited past the deadline");
    assert.match(out.remotes[0].error, /^not answering \(no complete answer within/);
  } finally {
    await slow.close();
  }
});
