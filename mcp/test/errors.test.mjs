// Runs against the compiled output: `npm run build && npm test`.
//
// #471: every tool appended "Is Quern running?" to every failure, including a
// 403 from a server that had answered and said why. The hint belongs only to a
// connection that was never made.
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

// The MCP reads ~/.quern at import time; point HOME at nothing so the real
// API key never reaches the test servers below.
process.env.HOME = mkdtempSync(join(tmpdir(), "quern-errors-"));
const { apiRequest, ServerRefused, ServerTimeout, ServerUnreachable } = await import("../dist/http.js");
const { errorText } = await import("../dist/tools/helpers.js");

const HINT = "Is the Quern server running?";

function serve(handler) {
  return new Promise((resolve) => {
    const server = createServer(handler);
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

async function closedPort() {
  const probe = await serve(() => {});
  const port = probe.address().port;
  await new Promise((r) => probe.close(r));
  return port;
}

async function failureOf(promise) {
  try {
    await promise;
  } catch (e) {
    return e;
  }
  assert.fail("the request succeeded");
}

test("a refusal from a server that answered is quoted, with no hint", async () => {
  const body = JSON.stringify({ detail: "POST /api/v1/proxy/stop is owner-only: a client key cannot use it" });
  const server = await serve((req, res) => { res.writeHead(403); res.end(body); });
  process.env.QUERN_SERVER_URL = `http://127.0.0.1:${server.address().port}`;
  try {
    const e = await failureOf(apiRequest("POST", "/api/v1/proxy/stop"));
    assert.ok(e instanceof ServerRefused);
    assert.equal(e.status, 403);
    assert.equal(e.message, `HTTP 403: ${body}`, "the message form callers match on changed");
    const text = errorText(e);
    assert.ok(text.includes("owner-only"), "the server's reason was lost");
    assert.ok(!text.includes(HINT), "a server that answered was called unreachable");
  } finally {
    server.close();
  }
});

test("a structured refusal reaches the agent whole", async () => {
  const body = JSON.stringify({ status: "obstructed", reason: "covered", covered_by: { label: "Heading" } });
  const server = await serve((req, res) => { res.writeHead(409); res.end(body); });
  process.env.QUERN_SERVER_URL = `http://127.0.0.1:${server.address().port}`;
  try {
    const text = errorText(await failureOf(apiRequest("POST", "/api/v1/device/ui/tap-element", undefined, {})));
    assert.equal(text, `Error: HTTP 409: ${body}`);
  } finally {
    server.close();
  }
});

test("a connection that was never made gets the hint, naming today's command", async () => {
  process.env.QUERN_SERVER_URL = `http://127.0.0.1:${await closedPort()}`;
  const e = await failureOf(apiRequest("GET", "/api/v1/logs/summary"));
  assert.ok(e instanceof ServerUnreachable, `got ${e?.name}: ${e?.message}`);
  assert.equal(e.code, "ECONNREFUSED");
  const text = errorText(e);
  assert.ok(text.includes(HINT));
  assert.ok(text.includes("quern start") && text.includes("ensure_server"));
  assert.ok(!text.includes("quern-debug-server"), "the hint names the prototype's command");
});

test("a raw fetch failure from a tool that fetches for itself is classified too", async () => {
  const port = await closedPort();
  const e = await failureOf(fetch(`http://127.0.0.1:${port}/api/v1/device/screenshot`));
  assert.ok(errorText(e).includes(HINT));
});

test("a server too slow to answer is not called unreachable", async () => {
  const server = await serve(() => { /* never answers */ });
  process.env.QUERN_SERVER_URL = `http://127.0.0.1:${server.address().port}`;
  try {
    const e = await failureOf(apiRequest("GET", "/api/v1/device/ui", undefined, undefined, 300));
    assert.ok(e instanceof ServerTimeout, `got ${e?.name}: ${e?.message}`);
    const text = errorText(e);
    assert.ok(!text.includes(HINT));
    assert.ok(text.includes("busy"));
  } finally {
    server.closeAllConnections?.();
    server.close();
  }
});

test("anything else is reported as itself", () => {
  assert.equal(errorText(new Error("label is required")), "Error: label is required");
  assert.equal(errorText("plain string"), "Error: plain string");
});

test("no tool builds its own error string any more", () => {
  // The route-audit pattern from #254: a new tool that inlines its own error
  // text, or the old hint, fails here instead of drifting.
  const dir = new URL("../src/tools/", import.meta.url);
  const offenders = [];
  for (const name of readdirSync(dir)) {
    if (!name.endsWith(".ts") || name === "helpers.ts") continue;
    const src = readFileSync(new URL(name, dir), "utf8");
    if (src.includes("Is Quern running")) offenders.push(`${name}: inlines the old hint`);
    if (src.includes("`Error: ${e instanceof Error ? e.message : String(e)}")) {
      offenders.push(`${name}: formats an error itself instead of errorText(e)`);
    }
  }
  assert.deepEqual(offenders, []);
});
