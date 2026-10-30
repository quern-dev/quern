// One use_remote scenario per process (module state is per process), driven by
// test/use-remote.test.mjs. Everything is fake: HOME is a temp dir, "local" is
// plain http on 127.0.0.1 and the remotes are https with a pinned test
// certificate; each answers the held session's WebSocket with "attached"
// unless told not to. Prints one JSON line of what happened.
import { createHash, X509Certificate } from "node:crypto";
import { createServer as http } from "node:http";
import { createServer as https } from "node:https";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [scenario, certDir] = process.argv.slice(2);
const tls = { key: readFileSync(join(certDir, "key.pem")), cert: readFileSync(join(certDir, "cert.pem")) };
const FP = new X509Certificate(tls.cert).fingerprint256;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const json = (status, data, delay = 0) => async () => [status, data, delay];
const base = (name) => ({
  "GET /health": json(200, { status: "ok", version: "9.9" }),
  // Each server's own token, so a token presented to the wrong one shows.
  "POST /api/v1/sessions": json(200, { resume_token: `t-${name}`, resumed: false }),
  "GET /api/v1/sessions/agent-1": json(200, { reservations: [] }),
});

function fake(name, secure, routes, { attach = true, dropFirstAfter = 0 } = {}) {
  let sockets = 0;
  const log = [];
  const handler = async (req, res) => {
    for await (const _ of req) { /* drain */ }
    log.push(`${req.method} ${req.url.split("?")[0]}`);
    const r = routes[`${req.method} ${req.url.split("?")[0]}`] ?? json(200, { from: name });
    const [status, data, delay] = await r();
    if (delay) await sleep(delay);
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(data));
  };
  const srv = secure ? https(tls, handler) : http(handler);
  srv.on("upgrade", (req, sock) => {
    log.push(`WS ${req.url} ${req.headers["x-quern-resume-token"]}`);
    if (!attach) { sock.destroy(); return; }
    const accept = createHash("sha1")
      .update(req.headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64");
    sock.write("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
      `Sec-WebSocket-Accept: ${accept}\r\n\r\n`);
    const payload = Buffer.from(JSON.stringify({ event: "attached" }));
    sock.write(Buffer.concat([Buffer.from([0x81, payload.length]), payload]));
    // A dropped connection, once: the client reconnects with its token.
    if (dropFirstAfter && ++sockets === 1) setTimeout(() => sock.destroy(), dropFirstAfter);
    sock.on("data", () => {});
    sock.on("error", () => {});
  });
  return new Promise((ok) => srv.listen(0, "127.0.0.1", () => ok({ name, log, port: srv.address().port })));
}

const home = mkdtempSync(join(tmpdir(), "quern-ur-"));
mkdirSync(join(home, ".quern"));
process.env.HOME = home;
process.env.QUERN_CLIENT_KEY = "k";
process.env.QUERN_SESSION = "agent-1";
delete process.env.QUERN_REMOTE;

const plans = {
  window: [{ "GET /api/v1/proxy/status": json(200, { from: "local" }) },
           { "GET /health": json(503, {}, 1500) }],
  "two-switches": [{}, { "GET /health": json(200, { status: "ok", version: "9.9" }, 1000) }],
  "open-fails": [{}, { "POST /api/v1/sessions": json(401, { detail: "key refused" }) }],
  "held-unknown": [{ "GET /api/v1/sessions/agent-1": json(503, { detail: "busy" }) }, {}],
  holding: [{ "GET /api/v1/sessions/agent-1": json(200, { reservations: [{ udid: "SIM-1" }] }),
              "DELETE /api/v1/sessions/agent-1/reservations": json(200, { freed: ["SIM-1"] }) }, {}],
  success: [{}, {}],
  // local's session opens slowly: the switch happens while it is in flight.
  "stale-start": [{ "POST /api/v1/sessions": json(200, { resume_token: "t-local", resumed: false }, 800) }, {}],
  "stale-early": [{ "POST /api/v1/sessions": json(200, { resume_token: "t-local", resumed: false }, 800) }, {}],
  hint: [{ "POST /api/v1/sessions/agent-1/reservations": async () => [409, { detail: globalThis.detail }] }, {}],
};
const [localRoutes, romulusRoutes] = plans[scenario];
const local = await fake("local", false, { ...base("local"), ...localRoutes });
const romulus = await fake("romulus", true, { ...base("romulus"), ...romulusRoutes },
  { dropFirstAfter: scenario === "stale-early" ? 1200 : 0 });
const deimos = await fake("deimos", true, base("deimos"));
process.env.QUERN_SERVER_URL = `http://127.0.0.1:${local.port}`;
writeFileSync(join(home, ".quern", "remotes.json"), JSON.stringify({ remotes: {
  romulus: { url: `https://127.0.0.1:${romulus.port}`, key: "rk", fingerprint: FP },
  deimos: { url: `https://127.0.0.1:${deimos.port}`, key: "dk", fingerprint: FP },
} }));

const DIST = new URL("../dist/", import.meta.url).href;
const sys = await import(`${DIST}tools/system.js`);
const ses = await import(`${DIST}tools/sessions.js`);
const held = await import(`${DIST}held-session.js`);
const cfg = await import(`${DIST}config.js`);
const httpm = await import(`${DIST}http.js`);
const tools = {};
const stub = { registerTool: (n, _d, h) => { tools[n] = h; } };
sys.registerSystemTools(stub);
ses.registerSessionTools(stub);
const text = (r) => ({ error: !!r.isError, text: r.content[0].text });
const current = () => cfg.currentRemote() ?? "local";
const result = {};

// Held on local first, as an MCP would be -- or, for the stale cases, with
// local's open still in flight when the switch comes.
if (scenario === "stale-early") {
  void held.openHeldSession();
} else {
  held.startHeldSession();
}
if (scenario.startsWith("stale")) await sleep(100);
else for (let i = 0; i < 50 && held.heldSessionState().state !== "live"; i++) await sleep(50);
result.startState = held.heldSessionState().state;

if (scenario === "window") {
  const switching = tools.use_remote({ name: "romulus" });
  await sleep(300);
  result.concurrent = (await httpm.apiRequest("GET", "/api/v1/proxy/status")).from;
  Object.assign(result, text(await switching));
} else if (scenario === "two-switches") {
  const a = tools.use_remote({ name: "romulus" });
  await sleep(100);
  result.b = text(await tools.use_remote({ name: "deimos" }));
  result.a = text(await a);
  result.next = (await httpm.apiRequest("GET", "/api/v1/proxy/status")).from;
} else if (scenario === "open-fails" || scenario === "success") {
  httpm.sessionNotices.push("about local");
  Object.assign(result, text(await tools.use_remote({ name: "romulus" })));
  result.notices = [...httpm.sessionNotices];
  result.next = (await httpm.apiRequest("GET", "/api/v1/proxy/status")).from;
} else if (scenario === "held-unknown" || scenario === "holding") {
  result.refused = text(await tools.use_remote({ name: "romulus" }));
  result.currentAfterRefusal = current();
  Object.assign(result, text(await tools.use_remote({ name: "romulus", release: true })));
} else if (scenario.startsWith("stale")) {
  Object.assign(result, text(await tools.use_remote({ name: "romulus" })));
  // local's open answers at ~900ms; romulus drops the socket at ~1200ms and
  // the client reconnects with whatever token it then has.
  await sleep(2500);
} else if (scenario === "hint") {
  writeFileSync(join(home, "quern"), `#!/bin/sh\ncat <<'J'\n${JSON.stringify({ remotes: [
    { name: "local", devices: [] },
    { name: "romulus", devices: [{ udid: "R1", name: "iPhone 15", os_version: "iOS 17.5",
      device_family: "iPhone", device_type: "simulator", free: true, state: "shutdown" }] },
  ] })}\nJ\n`);
  chmodSync(join(home, "quern"), 0o755);
  process.env.PATH = `${home}:/usr/bin:/bin`;
  globalThis.detail = "asked for 1 device(s), 0 free; 0 held by other sessions";
  result.noMatch = text(await tools.reserve_devices({ count: 1, os_version: "17.5" }));
  result.tooMany = text(await tools.reserve_devices({ count: 2, os_version: "17.5" }));
  result.bootedOnly = text(await tools.reserve_devices({ count: 1, os_version: "17.5", auto_boot: false }));
  globalThis.detail = "session agent-1 has no live connection";
  result.notLive = text(await tools.reserve_devices({ count: 1, os_version: "17.5" }));
}
result.current = current();
result.heldState = held.heldSessionState().state;
result.logs = { local: local.log, romulus: romulus.log, deimos: deimos.log };
console.log(JSON.stringify(result));
held.stopHeldSession();
setTimeout(() => process.exit(0), 100);
