// Runs against the compiled output: `npm run build && npm test`.
// The native list_remotes (#605): never the quern CLI, never this Mac's real
// server -- every server here is a fake on loopback, and the local one is
// injected.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { devices, diskRow, health, HttpStatus, loadTarget, probe, readRemotes, reason, remoteList,
  Unexpected }
  from "../dist/remote-status.js";
import { execFile } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
const FIXTURE = JSON.parse(readFileSync(join(here, "..", "..", "tests", "fixtures",
                                             "remote-status.json"), "utf-8"));

for (const c of FIXTURE.entries) {
  test(`shared fixture, entry: ${c.name} ${JSON.stringify(c.entry).slice(0, 40)}`, () => {
    const file = "/x/remotes.json";
    const got = loadTarget(c.name, c.entry, file);
    if (c.error) assert.equal(got, c.error.replaceAll("{file}", file));
    else assert.deepEqual(got, { name: c.name, ...c.target });
  });
}

for (const c of FIXTURE.reasons) {
  test(`shared fixture, reason: ${c.text.slice(0, 30)}`, () => {
    const e = c.http ? new HttpStatus(c.http) : new Unexpected(c.unexpected);
    assert.equal(reason(e), c.text);
  });
}

for (const c of FIXTURE.disk) {
  test(`shared fixture, disk: ${JSON.stringify(c.answer).slice(0, 40)}`, () => {
    assert.deepEqual(diskRow(c.answer), [c.disk, c.error]);
  });
}

for (const c of FIXTURE.health) {
  test(`shared fixture, health: ${JSON.stringify(c.answer)}`, () => {
    if (c.version === null) assert.throws(() => health(c.answer), /no quern health answer/);
    else assert.equal(health(c.answer), c.version);
  });
}

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
    const row = await probe(target(s.url));
    assert.equal(row.reachable, true);
    assert.equal(row.version, "0.25.0");
    assert.equal(row.free, 1);
    assert.equal(row.devices.length, 1);
    assert.equal(row.error, null);
    assert.ok(s.seen.every((r) => r.auth === "Bearer k-1" && r.session === "quern-remote-status"));
  } finally {
    await s.close();
  }
});

test("versions are compared with this Mac's server when it answers, else this MCP's own", async () => {
  const here = await fake(OK);                                        // 0.25.0
  const mine = target(here.url, { isLocal: true });
  try {
    const file = remotesFile();
    let out = await remoteList({ ownVersion: "9.9", file, localServer: () => mine });
    assert.equal(out.local_version, "0.25.0", "the local server's version wins over the MCP's");
    assert.equal(out.remotes[0].version_matches, true);
    await here.close();
    out = await remoteList({ ownVersion: "9.9", file, localServer: () => mine });
    assert.equal(out.local_version, "9.9", "not running: the MCP's own version");
    out = await remoteList({ ownVersion: null, file, localServer: () => mine });
    assert.equal(out.local_version, "unknown", "as the CLI prints it");
  } finally {
    await here.close().catch(() => {});
  }
});

test("something that is not quern is said, never read as a server with no devices", async () => {
  const s = await fake({ "/health": [200, "<html>router login</html>"] });
  try {
    const row = await probe(target(s.url));
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
    assert.match((await probe(target(s.url))).error, /^refused its key \(HTTP 401\)/);
  } finally {
    await s.close();
  }
});

test("health answered but devices failed: reachable, devices null, and why", async () => {
  const s = await fake({ "/health": OK["/health"], "/api/v1/device/list": [500, { detail: "x" }] });
  try {
    const row = await probe(target(s.url));
    assert.equal(row.reachable, true);
    assert.equal(row.devices, null);
    assert.equal(row.free, null);
    assert.equal(row.error, "devices could not be listed: HTTP 500");
  } finally {
    await s.close();
  }
});

test("disk use is read after the devices; a server from before it says not reported (#581)", async () => {
  const report = { used_bytes: 9, used_complete: true, disk: { free_bytes: 5, total_bytes: 20 },
                   warnings: [], measured_at: 2, stores: {} };
  const s = await fake({ ...OK, "/api/v1/system/disk": [200, report] });
  const old = await fake(OK);
  try {
    const row = await probe(target(s.url));
    assert.deepEqual(row.disk, { used_bytes: 9, used_complete: true, free_bytes: 5,
                                 total_bytes: 20, warnings: [], measured_at: 2 });
    assert.equal(row.disk_error, null);
    assert.equal(row.error, null);
    const before = await probe(target(old.url));
    assert.equal(before.disk, null);
    assert.equal(before.disk_error, "disk use could not be read: HTTP 404");
    assert.equal(before.free, 1, "the rest of the row still stands");
  } finally {
    await s.close();
    await old.close();
  }
});

test("devices that could not be listed: disk is not asked", async () => {
  const s = await fake({ "/health": OK["/health"], "/api/v1/device/list": [500, { detail: "x" }] });
  try {
    const row = await probe(target(s.url));
    assert.equal(row.disk, null);
    assert.equal(row.disk_error, null);
    assert.ok(!s.seen.some((r) => r.path === "/api/v1/system/disk"));
  } finally {
    await s.close();
  }
});

test("a deadline during the disk read keeps the version and devices", async () => {
  const s = await fake({ ...OK, "/api/v1/system/disk": () => {} });   // never answers
  try {
    const out = await remoteList({ file: remotesFile(), timeoutMs: 5000, deadlineMs: 400,
                                   localServer: () => target(s.url) });
    const row = out.remotes[0];
    assert.equal(row.reachable, true);
    assert.equal(row.free, 1);
    assert.equal(row.disk, null);
    assert.equal(row.disk_error, "disk use did not answer in time");
  } finally {
    await s.close();
  }
});

test("nothing listening is 'not reachable', and a slow server times out", async () => {
  const s = await fake({});
  const url = s.url;
  await s.close();
  assert.match((await probe(target(url))).error, /^not reachable \(ECONNREFUSED\)$/);
  const slow = await fake({ "/health": () => {} });   // never answers
  try {
    const row = await probe(target(slow.url), 200);
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
  // A file that exists and cannot be read (here, a directory) is not "none".
  assert.throws(() => readRemotes(mkdtempSync(join(tmpdir(), "quern-rs-dir-"))), /is not readable/);
  assert.throws(() => readRemotes(remotesFile({ remotes: [] })), /no 'remotes' object/);
});

test("an entry that cannot be used says why: http, no key, a bad fingerprint", () => {
  assert.match(loadTarget("a", { url: "http://x", key: "k", fingerprint: FP }), /not an https:\/\//);
  assert.match(loadTarget("a", { url: "https://x", fingerprint: FP }), /incomplete \('key'\)/);
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
    const out = await remoteList({ ownVersion: "0.25.0", file, timeoutMs: 1000,
                                   localServer: () => target(s.url, { isLocal: true }) });
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

test("this Mac's server failing its health check in any way reads 'not running here'", async () => {
  const gone = await fake({});
  const url = gone.url;
  await gone.close();
  const junk = await fake({ "/health": [200, "<html>"] });
  try {
    for (const u of [url, junk.url]) {
      const out = await remoteList({ file: remotesFile(), localServer: () => target(u, { isLocal: true }) });
      assert.equal(out.remotes[0].error, "not running here (quern start)");
      assert.equal(out.remotes[0].local, true);
    }
  } finally {
    await junk.close();
  }
});

test("a remote's failure keeps its own reason: only this Mac's reads 'not running'", async () => {
  const gone = await fake({});
  const url = gone.url;
  await gone.close();
  const out = await remoteList({ file: remotesFile(), localServer: () => target(url, { name: "x" }) });
  assert.equal(out.remotes[0].error, "not reachable (ECONNREFUSED)");
});

test("a remote paired by hand as 'local' is not this Mac", async () => {
  const file = remotesFile({ remotes: { local: { url: "https://127.0.0.1:9", key: "k",
                                                 fingerprint: FP, label: "studio" } } });
  const out = await remoteList({ file, timeoutMs: 300, localServer: () => ({ name: "local",
    label: "this Mac", reachable: false, error: "not running here (quern start)" }) });
  assert.deepEqual(out.remotes.map((r) => [r.name, r.label, r.local]),
                   [["local", "this Mac", true], ["local", "studio", false]]);
});

/** local() in a child, whose HOME holds the state.json given: config.ts fixes
 * its paths when it loads. `pid` is what the port lookup answers. */
function runLocal({ state, pid, env = {} }) {
  const home = mkdtempSync(join(tmpdir(), "quern-local-"));
  mkdirSync(join(home, ".quern"));
  if (state) writeFileSync(join(home, ".quern", "state.json"), JSON.stringify(state));
  writeFileSync(join(home, ".quern", "api-key"), "owner-key\n");
  const mod = new URL("../dist/remote-status.js", import.meta.url).href;
  const code = `const m = await import(${JSON.stringify(mod)});` +
    `console.log(JSON.stringify(await m.local(async () => ${JSON.stringify(pid ?? null)})));`;
  return new Promise((ok, no) => execFile(process.execPath, ["--input-type=module", "-e", code],
    { env: { PATH: process.env.PATH, HOME: home, ...env }, timeout: 15_000 },
    (e, out, err) => (e ? no(new Error(String(err))) : ok(JSON.parse(String(out))))));
}

test("local(): state.json's port on loopback, the owner's key, QUERN_SERVER_URL ignored", async () => {
  const t = await runLocal({ state: { server_port: 9110, pid: 42 }, pid: 42,
                             env: { QUERN_SERVER_URL: "http://elsewhere.example:1" } });
  assert.equal(t.url, "http://127.0.0.1:9110");
  assert.equal(t.key, "owner-key");
  assert.equal(t.isLocal, true);
  const c = await runLocal({ state: { server_port: 9110, pid: 42 }, pid: null,
                             env: { QUERN_CLIENT_KEY: " qc_k " } });
  assert.equal(c.key, "qc_k", "QUERN_CLIENT_KEY, as config.client_api_key; an unknown listener is asked");
});

test("local(): no state, a bad port, or another process on the port is not running here", async () => {
  for (const args of [{}, { state: { server_port: "9110", pid: 42 } },
                      { state: { server_port: 70000 } },
                      { state: { server_port: 9110, pid: 42 }, pid: 99 }]) {
    const t = await runLocal(args);
    assert.equal(t.error, "not running here (quern start)", JSON.stringify(args));
    assert.equal(t.key, undefined, "no key for an impostor");
  }
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
