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
