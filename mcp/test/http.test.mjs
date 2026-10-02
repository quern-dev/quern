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
