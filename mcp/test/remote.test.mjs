// A remote quern (#254, phase 3): the MCP's pinned connections, against a real
// local TLS server with a throwaway certificate.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, X509Certificate } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:https";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";

import { serverFetch } from "../dist/http.js";
import { loadRemote, PinMismatch, showFingerprint } from "../dist/remote.js";

let dir;
let server;
let port;
let fingerprint;
let requests = 0;

before(async () => {
  dir = mkdtempSync(join(tmpdir(), "quern-remote-"));
  execFileSync("openssl", ["req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
    "-nodes", "-keyout", join(dir, "key.pem"), "-out", join(dir, "cert.pem"), "-days", "1",
    "-subj", "/CN=quern test"], { stdio: "ignore" });
  const pem = readFileSync(join(dir, "cert.pem"));
  fingerprint = createHash("sha256").update(new X509Certificate(pem).raw).digest("hex");
  server = createServer({ key: readFileSync(join(dir, "key.pem")), cert: pem }, (req, res) => {
    requests += 1;
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok", auth: req.headers.authorization ?? null }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  port = server.address().port;
});

after(() => {
  server?.close();
  rmSync(dir, { recursive: true, force: true });
});

test("a pinned connection to the paired certificate goes through", async () => {
  const resp = await serverFetch({ fingerprint }, `https://127.0.0.1:${port}/health`);
  assert.equal(resp.status, 200);
  assert.equal((await resp.json()).status, "ok");
});

test("a wrong pin is refused before anything is sent, naming both fingerprints", async () => {
  const before = requests;
  const wrong = "0".repeat(64);
  await assert.rejects(
    serverFetch({ fingerprint: wrong }, `https://127.0.0.1:${port}/health`, {
      headers: { Authorization: "Bearer qc_secret" },
    }),
    (e) => {
      assert.ok(e instanceof PinMismatch, `${e}`);
      assert.match(e.message, new RegExp(showFingerprint(fingerprint).slice(0, 20)));
      return true;
    },
  );
  assert.equal(requests, before, "no request reached the server: the key never left");
});

test("loadRemote reads a paired server and refuses an unpaired name", () => {
  const file = join(dir, "remotes.json");
  writeFileSync(file, JSON.stringify({ remotes: { office: {
    url: "https://ci.lan:9130/", key: "qc_k", fingerprint: showFingerprint(fingerprint) } } }));
  const remote = loadRemote("office", file);
  assert.equal(remote.url, "https://ci.lan:9130");
  assert.equal(remote.fingerprint, fingerprint);
  assert.throws(() => loadRemote("nowhere", file), /quern connect/);
});

test("QUERN_REMOTE that cannot be read does not fall back to this Mac's server", async () => {
  const { execFile } = await import("node:child_process");
  const { fileURLToPath } = await import("node:url");
  const config = fileURLToPath(new URL("../dist/config.js", import.meta.url));
  const home = mkdtempSync(join(tmpdir(), "quern-remote-home-"));
  try {
    const out = await new Promise((resolve) => execFile(process.execPath, ["--input-type=module", "-e",
      `const m = await import(${JSON.stringify(config)}); try { m.discoverServer(); console.log("FELL BACK"); } catch (e) { console.log("REFUSED " + e.message); }`],
      { env: { ...process.env, HOME: home, QUERN_REMOTE: "office" } },
      (err, stdout) => resolve(stdout)));
    assert.match(out, /^REFUSED/, out);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});
