// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { test } from "node:test";

import { requestWithoutTimeout } from "../dist/http.js";

function serve(handler) {
  return new Promise((resolve) => {
    const server = createServer(handler);
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

test("a delayed answer arrives, with its body and status", async () => {
  const server = await serve((req, res) => {
    let got = "";
    req.on("data", (c) => (got += c));
    req.on("end", () =>
      setTimeout(() => {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ echoed: JSON.parse(got), auth: req.headers.authorization }));
      }, 300)
    );
  });
  try {
    const url = new URL(`http://127.0.0.1:${server.address().port}/x`);
    const r = await requestWithoutTimeout(
      url, "POST", { Authorization: "Bearer k", "Content-Type": "application/json" },
      JSON.stringify({ a: 1 }));
    assert.equal(r.status, 200);
    assert.deepEqual(JSON.parse(r.text), { echoed: { a: 1 }, auth: "Bearer k" });
  } finally {
    server.close();
  }
});

test("an error status is returned, not swallowed", async () => {
  const server = await serve((req, res) => {
    res.writeHead(400);
    res.end("variant is required");
  });
  try {
    const url = new URL(`http://127.0.0.1:${server.address().port}/x`);
    const r = await requestWithoutTimeout(url, "GET", {});
    assert.deepEqual(r, { status: 400, text: "variant is required" });
  } finally {
    server.close();
  }
});

test("a refused connection rejects", async () => {
  const server = await serve(() => {});
  const port = server.address().port;
  await new Promise((r) => server.close(r));
  await assert.rejects(
    requestWithoutTimeout(new URL(`http://127.0.0.1:${port}/x`), "GET", {}),
    /ECONNREFUSED/);
});

// ── session attribution (#254) ───────────────────────────────────────────────

test("a session id is minted per process, in the server's format", async () => {
  const { SESSION_ID, SESSION_ID_PATTERN, mintSessionId } = await import("../dist/http.js");
  assert.match(SESSION_ID, SESSION_ID_PATTERN);
  assert.match(mintSessionId(undefined), /^mcp-[0-9a-f]{8}$/);
  assert.notEqual(mintSessionId(undefined), mintSessionId(undefined));
});

test("QUERN_SESSION names it, unless the server would refuse the name", async () => {
  const { mintSessionId } = await import("../dist/http.js");
  assert.equal(mintSessionId("ci-run:4471"), "ci-run:4471");
  assert.equal(mintSessionId("  padded  "), "padded");
  // Refused names fall back to a minted id rather than being sent and then
  // discarded server-side, which would leave the caller unattributed.
  assert.match(mintSessionId("has space"), /^mcp-[0-9a-f]{8}$/);
  assert.match(mintSessionId("x".repeat(65)), /^mcp-[0-9a-f]{8}$/);
  assert.match(mintSessionId("line\nbreak"), /^mcp-[0-9a-f]{8}$/);
});

test("every API request carries the session header", async () => {
  const seen = [];
  const server = await serve((req, res) => {
    seen.push(req.headers["x-quern-session"]);
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end("{}");
  });
  const before = process.env.QUERN_SERVER_URL;
  try {
    process.env.QUERN_SERVER_URL = `http://127.0.0.1:${server.address().port}`;
    const { apiRequest, SESSION_ID } = await import("../dist/http.js");
    await apiRequest("GET", "/api/v1/trace");
    await apiRequest("POST", "/api/v1/device/ui/tap", undefined, { x: 1, y: 2 });
    await apiRequest("POST", "/api/v1/builds", undefined, {}, "none");
    assert.deepEqual(seen, [SESSION_ID, SESSION_ID, SESSION_ID]);
  } finally {
    if (before === undefined) delete process.env.QUERN_SERVER_URL;
    else process.env.QUERN_SERVER_URL = before;
    server.close();
  }
});

test("QUERN_CLIENT_KEY is the key sent, over the owner's (#254, phase 2)", async () => {
  const seen = [];
  const server = await serve((req, res) => {
    seen.push(req.headers.authorization);
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end("{}");
  });
  const before = { url: process.env.QUERN_SERVER_URL, key: process.env.QUERN_CLIENT_KEY };
  try {
    process.env.QUERN_SERVER_URL = `http://127.0.0.1:${server.address().port}`;
    const { apiRequest } = await import("../dist/http.js");
    process.env.QUERN_CLIENT_KEY = "qc_client";
    await apiRequest("GET", "/api/v1/trace");
    assert.deepEqual(seen, ["Bearer qc_client"]);
  } finally {
    for (const [name, value] of [["QUERN_SERVER_URL", before.url], ["QUERN_CLIENT_KEY", before.key]]) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    server.close();
  }
});

test("a blank QUERN_CLIENT_KEY means the owner's key, not an empty one", async () => {
  // In a child process with its own HOME and owner key: the key file's path is
  // fixed when config.js loads, so it cannot be redirected from in here, and
  // the developer's real ~/.quern must not decide the result (CodeRabbit).
  const { execFile } = await import("node:child_process");
  const { mkdtempSync, mkdirSync, writeFileSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { fileURLToPath } = await import("node:url");
  const home = mkdtempSync(join(tmpdir(), "quern-key-"));
  mkdirSync(join(home, ".quern"));
  writeFileSync(join(home, ".quern", "api-key"), "owner-key\n");
  const seen = [];
  const server = await serve((req, res) => {
    seen.push(req.headers.authorization);
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end("{}");
  });
  try {
    const http = fileURLToPath(new URL("../dist/http.js", import.meta.url));
    const env = { ...process.env, HOME: home, QUERN_CLIENT_KEY: "   ",
                  QUERN_SERVER_URL: `http://127.0.0.1:${server.address().port}` };
    delete env.QUERN_STATE_DIR;
    await new Promise((resolve, reject) => execFile(process.execPath, ["--input-type=module", "-e",
      `const m = await import(${JSON.stringify(http)}); await m.apiRequest("GET", "/api/v1/trace");`],
      { env }, (err) => (err ? reject(err) : resolve())));
    assert.deepEqual(seen, ["Bearer owner-key"]);
  } finally {
    server.close();
    rmSync(home, { recursive: true, force: true });
  }
});

test("the module loads with QUERN_SESSION set", async () => {
  // SESSION_ID is computed at import. The first version read the pattern
  // before it was declared, which only bites when QUERN_SESSION is set -- so
  // this imports in a fresh process with it set, as a CI job would start one.
  const { execFileSync } = await import("node:child_process");
  const { fileURLToPath } = await import("node:url");
  const http = fileURLToPath(new URL("../dist/http.js", import.meta.url));
  const out = execFileSync(process.execPath, ["--input-type=module", "-e",
    `const m = await import(${JSON.stringify(http)}); console.log(m.SESSION_ID);`],
    { env: { ...process.env, QUERN_SESSION: "ci-run-4471" }, encoding: "utf8" });
  assert.equal(out.trim(), "ci-run-4471");
});

test("under Claude Code the session is the agent's own session id", async () => {
  const { mintSessionId } = await import("../dist/http.js");
  const claude = "1256bb48-e4fc-5e5c-95cc-3a3bde6898a3";
  assert.equal(mintSessionId(undefined, claude), `claude-${claude}`);
  assert.equal(mintSessionId("ci-run-4471", claude), "ci-run-4471", "QUERN_SESSION wins");
  assert.equal(mintSessionId("bad name", claude), `claude-${claude}`);
  assert.match(mintSessionId(undefined, "bad id"), /^mcp-[0-9a-f]{8}$/);
  assert.match(mintSessionId(undefined, "   "), /^mcp-[0-9a-f]{8}$/);
});

test("the module picks up CLAUDE_CODE_SESSION_ID when it loads", async () => {
  const { execFileSync } = await import("node:child_process");
  const { fileURLToPath } = await import("node:url");
  const http = fileURLToPath(new URL("../dist/http.js", import.meta.url));
  const env = { ...process.env, CLAUDE_CODE_SESSION_ID: "abc-123" };
  delete env.QUERN_SESSION;
  const out = execFileSync(process.execPath, ["--input-type=module", "-e",
    `const m = await import(${JSON.stringify(http)}); console.log(m.SESSION_ID);`],
    { env, encoding: "utf8" });
  assert.equal(out.trim(), "claude-abc-123");
});

test("API headers are built only in http.ts", async () => {
  // take_screenshot and take_annotated_screenshot called fetch with their own
  // Authorization header and no session, so two of the commonest actions were
  // unattributed while the apiRequest test passed. Any request that builds
  // its own auth header would skip the session the same way.
  const { readdirSync, readFileSync, statSync } = await import("node:fs");
  const { join } = await import("node:path");
  const { fileURLToPath } = await import("node:url");
  const src = fileURLToPath(new URL("../src", import.meta.url));
  const offenders = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (name.endsWith(".ts") && name !== "http.ts"
               && /Authorization|Bearer \$\{/.test(readFileSync(path, "utf8"))) {
        offenders.push(path.slice(src.length + 1));
      }
    }
  };
  walk(src);
  assert.deepEqual(offenders, [], "build headers with quernHeaders() from http.ts");
});
