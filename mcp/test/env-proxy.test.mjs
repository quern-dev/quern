// Runs against the compiled output: `npm run build && npm test`.
// Requests to this Mac's server never go through a proxy from the environment,
// which would be handed the key (#607 review). Node reads the proxy settings
// at startup, so each request runs in a child started with them set.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createServer } from "node:http";
import { test } from "node:test";

const HTTP = new URL("../dist/http.js", import.meta.url).href;

async function listen(handler) {
  const s = createServer(handler);
  await new Promise((r) => s.listen(0, "127.0.0.1", r));
  return s;
}

for (const how of ["serverFetch", "requestWithoutTimeout"]) {
  test(`${how} to the local server ignores HTTP_PROXY under NODE_USE_ENV_PROXY`, async () => {
    const hits = [];
    const proxy = await listen((req, res) => { hits.push("proxy"); res.end("{}"); });
    proxy.on("connect", (req, sock) => { hits.push("proxy"); sock.end(); });
    const target = await listen((req, res) => { hits.push(`target ${req.headers.authorization}`); res.end("{}"); });
    const url = `http://127.0.0.1:${target.address().port}/health`;
    const code = how === "serverFetch"
      ? `const h = await import(${JSON.stringify(HTTP)}); await (await h.serverFetch({}, ${JSON.stringify(url)}, { headers: { Authorization: "Bearer k" } })).text();`
      : `const h = await import(${JSON.stringify(HTTP)}); await h.requestWithoutTimeout(new URL(${JSON.stringify(url)}), "GET", { Authorization: "Bearer k" });`;
    const env = { PATH: process.env.PATH, HOME: process.env.HOME, NODE_USE_ENV_PROXY: "1",
                  HTTP_PROXY: `http://127.0.0.1:${proxy.address().port}`,
                  http_proxy: `http://127.0.0.1:${proxy.address().port}` };
    try {
      await new Promise((ok, no) => execFile(process.execPath, ["--input-type=module", "-e", code],
        { env, timeout: 15_000 }, (e, out, err) => (e ? no(new Error(String(err) || e.message)) : ok())));
      assert.deepEqual(hits, ["target Bearer k"]);
    } finally {
      proxy.close();
      target.close();
    }
  });
}
