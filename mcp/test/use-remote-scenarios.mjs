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
  const bodies = [];
  const handler = async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    log.push(`${req.method} ${req.url.split("?")[0]}`);
    if (raw) bodies.push({ path: `${req.method} ${req.url.split("?")[0]}`, body: JSON.parse(raw) });
    const r = routes[`${req.method} ${req.url.split("?")[0]}`] ?? json(200, { from: name });
    const [status, data, delay] = await r(raw ? JSON.parse(raw) : undefined);
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
  return new Promise((ok) => srv.listen(0, "127.0.0.1", () => ok({ name, log, bodies, port: srv.address().port })));
}

const home = mkdtempSync(join(tmpdir(), "quern-ur-"));
mkdirSync(join(home, ".quern"));
process.env.HOME = home;
process.env.QUERN_CLIENT_KEY = "k";
process.env.QUERN_SESSION = "agent-1";
delete process.env.QUERN_REMOTE;

function mockIds(name) {
  let n = 0;
  return async () => [200, { status: "accepted", rule_id: `mock_${name}${++n}` }];
}

// One counter each, made once: made per request, every rule was mock_romulus1.
const retryIds = mockIds("romulus");
const partialIds = mockIds("romulus");

const plans = {
  window: [{ "GET /api/v1/proxy/status": json(200, { from: "local" }) },
           { "GET /health": json(503, {}, 1500) }],
  "two-switches": [{}, { "GET /health": json(200, { status: "ok", version: "9.9" }, 1000) }],
  "open-fails": [{}, { "POST /api/v1/sessions": json(401, { detail: "key refused" }) }],
  "held-unknown": [{ "GET /api/v1/sessions/agent-1": json(503, { detail: "busy" }) }, {}],
  holding: [{ "GET /api/v1/sessions/agent-1": json(200, { reservations: [{ udid: "SIM-1" }] }),
              "DELETE /api/v1/sessions/agent-1/reservations": json(200, { freed: ["SIM-1"] }) }, {}],
  success: [{}, {}],
  "reserve-during-switch": [{}, { "GET /health": json(200, { status: "ok", version: "9.9" }, 1000) }],
  // local's session opens slowly: the switch happens while it is in flight.
  "stale-start": [{ "POST /api/v1/sessions": json(200, { resume_token: "t-local", resumed: false }, 800) }, {}],
  "stale-early": [{ "POST /api/v1/sessions": json(200, { resume_token: "t-local", resumed: false }, 800) }, {}],
  // What the agent set up follows it (carried.ts). Mock ids are handed out
  // in order; romulus's proxy status, mock creation and capture vary by case.
  carry: [{ "POST /api/v1/proxy/mocks": mockIds("local"),
            "DELETE /api/v1/proxy/mocks/mock_local1": json(200, {}),
            "DELETE /api/v1/proxy/mocks/mock_local2": json(404, { detail: "gone" }),
            "DELETE /api/v1/proxy/mocks/mock_local3": json(500, { detail: "boom" }) },
          { "POST /api/v1/proxy/mocks": mockIds("romulus"),
            "GET /api/v1/proxy/status": json(200, { status: "running", local_capture: ["Metatext"] }),
            "POST /api/v1/sessions/agent-1/reservations": async (b) => [200, { reserved: globalThis.reserved }] }],
  // #582: a mock scoped to an Android device says so, and moves to Android.
  "carry-android": [{ "POST /api/v1/proxy/mocks": async (b) => [200, {
                        status: "accepted", rule_id: b.simulator_udid === "emulator-5554" ? "mock_emu" : "mock_sim",
                        ...(b.simulator_udid === "emulator-5554" ? { platform: "android" } : {}) }] },
                    { "POST /api/v1/proxy/mocks": mockIds("romulus"),
                      "POST /api/v1/sessions/agent-1/reservations": async () => [200, { reserved: globalThis.reserved }] }],
  "carry-android-rescope": [{ "POST /api/v1/proxy/mocks": mockIds("local"),
                              "PATCH /api/v1/proxy/mocks/mock_local2": json(200, { platform: "android" }) },
                            { "POST /api/v1/proxy/mocks": mockIds("romulus"),
                              "POST /api/v1/sessions/agent-1/reservations": async () => [200, { reserved: globalThis.reserved }] }],
  // #582: an Android device routed through the proxy is routed again after a switch.
  "carry-proxy": [{ "POST /api/v1/proxy/device-proxy-config": json(200, { applied: true }) },
                  { "POST /api/v1/proxy/device-proxy-config": async (b) => [200, {
                      applied: true, https: { ca_install: "manual", ca_trusted: null, note: "n" } }],
                    "POST /api/v1/sessions/agent-1/reservations": async () => [200, { reserved: globalThis.reserved }] }],
  "carry-proxy-partial": [{ "POST /api/v1/proxy/device-proxy-config": json(200, { applied: true }) },
                          { "POST /api/v1/proxy/device-proxy-config": async (b) =>
                              b.udid === "emulator-5558" && !globalThis.fixed ? [502, { detail: "adb offline" }]
                                : [200, { applied: true }],
                            "POST /api/v1/sessions/agent-1/reservations": async () => [200, { reserved: globalThis.reserved }] }],
  "carry-retry": [{ "POST /api/v1/proxy/mocks": mockIds("local") },
                  { "POST /api/v1/proxy/mocks": async (b) => globalThis.mocksDown
                      ? [503, { detail: "proxy is not running" }] : retryIds(b),
                    "GET /api/v1/proxy/status": json(200, { status: "stopped" }),
                    "POST /api/v1/sessions/agent-1/reservations": async () => [200, { reserved: globalThis.reserved }] }],
  // Review findings on the carry record.
  "carry-hops": [{ "POST /api/v1/proxy/mocks": mockIds("local"),
                   "PATCH /api/v1/proxy/mocks/mock_local1": json(200, {}),
                   "DELETE /api/v1/proxy/mocks/mock_local2": json(404, { detail: "no such rule" }) },
                 { "POST /api/v1/proxy/mocks": mockIds("romulus"),
                   "POST /api/v1/sessions/agent-1/reservations": async () => [200, { reserved: globalThis.reserved }],
                   "DELETE /api/v1/sessions/agent-1/reservations": async () => [200, { freed: globalThis.freed ?? [] }],
                   "GET /api/v1/sessions/agent-1": json(200, { reservations: [] }) }],
  "carry-partial": [{ "POST /api/v1/proxy/mocks": mockIds("local") },
                    // Capture fails on SIM-R2 the first time, as mocks do.
                    { "POST /api/v1/proxy/mocks": async (b) => globalThis.failR2 && b?.simulator_udid === "SIM-R2"
                        ? [503, { detail: "busy" }] : partialIds(b),
                      "POST /api/v1/proxy/session-capture": async (b) => globalThis.failR2 && b?.simulator_udid === "SIM-R2"
                        ? [503, { detail: "busy" }] : [200, {}],
                      "POST /api/v1/sessions/agent-1/reservations": async () => [200, { reserved: globalThis.reserved }] }],
  // The old server is slow to give up its mock: the switch is still moving
  // it when the agent sets a new one.
  "carry-race": [{ "POST /api/v1/proxy/mocks": mockIds("local"),
                   "DELETE /api/v1/proxy/mocks/mock_local1": json(200, {}, 1000) },
                 { "POST /api/v1/proxy/mocks": mockIds("romulus"),
                   "POST /api/v1/sessions/agent-1/reservations": async () => [200, { reserved: globalThis.reserved }] }],
  // The old server stops answering on the first delete.
  "carry-dead-old": [{ "POST /api/v1/proxy/mocks": mockIds("local"),
                       "DELETE /api/v1/proxy/mocks/mock_local1": json(200, {}, 6000) }, {}],
  // The server makes the rule and still answers with an error (as a timed-out
  // request can have): a retry must adopt it, not make a second.
  "carry-ambiguous": [{ "POST /api/v1/proxy/mocks": mockIds("local") },
                      { "POST /api/v1/proxy/mocks": async (b) => {
                          const id = `mock_romulus${globalThis.made.length + 1}`;
                          globalThis.made.push({ rule_id: id, pattern: b.pattern, simulator_udid: b.simulator_udid ?? null,
                                                 response: { status_code: 200, headers: { "content-type": "application/json" }, ...b.response } });
                          return globalThis.made.length === 1 ? [500, { detail: "timed out" }] : [200, { rule_id: id }];
                        },
                        "GET /api/v1/proxy/mocks": async () => [200, { rules: globalThis.made, total: globalThis.made.length }],
                        "POST /api/v1/sessions/agent-1/reservations": async () => [200, { reserved: globalThis.reserved }] }],
  // local stays up but refuses to give up its rules at the first switch; on the
  // way back one is removed, one is gone already, one refuses again.
  "carry-stranded": [{ "POST /api/v1/proxy/mocks": mockIds("local"),
                       "DELETE /api/v1/proxy/mocks/mock_local1": async () => globalThis.back ? [200, {}] : [503, { detail: "busy" }],
                       "DELETE /api/v1/proxy/mocks/mock_local2": async () => globalThis.back ? [404, { detail: "no such rule" }] : [503, { detail: "busy" }],
                       "DELETE /api/v1/proxy/mocks/mock_local3": async () => globalThis.local3ok ? [200, {}] : [503, { detail: "busy" }],
                       "POST /api/v1/sessions/agent-1/reservations": async () => [200, { reserved: globalThis.reserved }] }, {}],
  // On the way back the first delete hangs: the rest are not tried then.
  "carry-stranded-stall": [{ "POST /api/v1/proxy/mocks": mockIds("local"),
                             "DELETE /api/v1/proxy/mocks/mock_local1": async () => globalThis.back ? [200, {}, 6000] : [503, {}],
                             "DELETE /api/v1/proxy/mocks/mock_local2": json(503, {}),
                             "DELETE /api/v1/proxy/mocks/mock_local3": json(503, {}) }, {}],
  // local stops answering once its mocks are set: the session check times out.
  "leave-unreachable": [{ "POST /api/v1/proxy/mocks": mockIds("local"),
                          "GET /api/v1/sessions/agent-1": async () => globalThis.hang ? [200, { reservations: [] }, 6000] : [200, { reservations: [] }] }, {}],
  // local answers, but with an error: not unreachable, so it is still asked.
  "leave-erroring": [{ "POST /api/v1/proxy/mocks": mockIds("local"),
                       "GET /api/v1/sessions/agent-1": async () => globalThis.hang ? [503, { detail: "busy" }] : [200, { reservations: [] }],
                       "DELETE /api/v1/proxy/mocks/mock_local1": json(200, {}) }, {}],
  hint: [{ "POST /api/v1/sessions/agent-1/reservations": async () => [409, { detail: globalThis.detail }],
           "GET /api/v1/device/list": json(200, { devices: [], tools: {}, active_udid: null }) }, {}],
};
const [localRoutes, romulusRoutes] = plans[scenario];
const local = await fake("local", false, { ...base("local"), ...localRoutes });
const romulus = await fake("romulus", true, { ...base("romulus"), ...romulusRoutes },
  { dropFirstAfter: scenario === "stale-early" ? 1200 : 0 });
const deimos = await fake("deimos", true, base("deimos"));
process.env.QUERN_SERVER_URL = `http://127.0.0.1:${local.port}`;
writeFileSync(join(home, ".quern", "remotes.json"), JSON.stringify({ remotes: {
  romulus: { url: `https://127.0.0.1:${romulus.port}`,
             // A client key, as a paired remote's is -- except where the scenario
             // sets a mock on a device it does not hold, which only the owner may.
             key: scenario.startsWith("carry") && scenario !== "carry-race" ? "qc_rk" : "rk",
             fingerprint: FP },
  deimos: { url: `https://127.0.0.1:${deimos.port}`, key: "dk", fingerprint: FP },
} }));

const DIST = new URL("../dist/", import.meta.url).href;
const sys = await import(`${DIST}tools/system.js`);
const ses = await import(`${DIST}tools/sessions.js`);
const dev = await import(`${DIST}tools/device.js`);
const icp = await import(`${DIST}tools/intercept.js`);
const prx = await import(`${DIST}tools/proxy.js`);
const held = await import(`${DIST}held-session.js`);
const cfg = await import(`${DIST}config.js`);
const httpm = await import(`${DIST}http.js`);
const tools = {};
const stub = { registerTool: (n, _d, h) => { tools[n] = h; } };
sys.registerSystemTools(stub);
ses.registerSessionTools(stub);
dev.registerDeviceTools(stub);
icp.registerInterceptTools(stub);
prx.registerProxyTools(stub);
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
} else if (scenario === "reserve-during-switch") {
  const switching = tools.use_remote({ name: "romulus" });
  await sleep(300);
  result.reserve = text(await tools.reserve_devices({ udids: ["X"] }));
  Object.assign(result, text(await switching));
} else if (scenario.startsWith("stale")) {
  Object.assign(result, text(await tools.use_remote({ name: "romulus" })));
  // local's open answers at ~900ms; romulus drops the socket at ~1200ms and
  // the client reconnects with whatever token it then has.
  await sleep(2500);
} else if (scenario === "carry") {
  sys.whenSwitched(async () => ({ kind: "session_loaded", app: "com.example" }));
  await tools.set_mock({ pattern: "~u /a", simulator_udid: "SIM-L", status_code: 200, body: "A" });
  await tools.set_mock({ pattern: "~u /b", status_code: 500, body: "B" });
  await tools.set_mock({ pattern: "~u /c", status_code: 200, body: "C" });
  await tools.update_mock({ rule_id: "mock_local1", body: "A2" });
  await tools.add_session_capture({ simulator_udid: "SIM-L", processes: ["Metatext"] });
  result.switched = JSON.parse(text(await tools.use_remote({ name: "romulus" })).text);
  globalThis.reserved = [{ udid: "PHONE-R", device_type: "device" }];
  result.noSimulator = JSON.parse(text(await tools.reserve_devices({ udids: ["PHONE-R"] })).text);
  globalThis.reserved = [{ udid: "SIM-R1", device_type: "simulator" }, { udid: "SIM-R2", device_type: "simulator" }];
  result.reserved = JSON.parse(text(await tools.reserve_devices({ count: 2 })).text);
  globalThis.reserved = [{ udid: "SIM-R3", device_type: "simulator" }];
  result.again = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-R3"] })).text);
} else if (scenario === "carry-android") {
  await tools.set_mock({ pattern: "~u /emu", simulator_udid: "emulator-5554", status_code: 200, body: "E" });
  await tools.set_mock({ pattern: "~u /sim", simulator_udid: "SIM-L", status_code: 200, body: "S" });
} else if (scenario === "carry-proxy") {
  await tools.record_device_proxy_config({ udid: "emulator-5554", apply: true });
  result.switched = JSON.parse(text(await tools.use_remote({ name: "romulus" })).text);
  globalThis.reserved = [{ udid: "SIM-R1", device_type: "simulator" }];
  result.onSimulator = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-R1"] })).text);
  globalThis.reserved = [{ udid: "emulator-5556", device_type: "android_emulator" }];
  result.onEmulator = JSON.parse(text(await tools.reserve_devices({ udids: ["emulator-5556"] })).text);
} else if (scenario === "carry-android-rescope") {
  await tools.set_mock({ pattern: "~u /all", status_code: 200, body: "ALL" });
  await tools.set_mock({ pattern: "~u /re", simulator_udid: "SIM-L", status_code: 200, body: "RE" });
  await tools.update_mock({ rule_id: "mock_local2", simulator_udid: "emulator-5554" });
  await tools.use_remote({ name: "romulus" });
  globalThis.reserved = [{ udid: "emulator-5556", device_type: "android_emulator" }];
  result.onEmulator = JSON.parse(text(await tools.reserve_devices({ udids: ["emulator-5556"] })).text);
  result.again = JSON.parse(text(await tools.reserve_devices({ udids: ["emulator-5556"] })).text);
} else if (scenario === "carry-proxy-partial") {
  await tools.record_device_proxy_config({ udid: "emulator-5554", apply: true });
  await tools.use_remote({ name: "romulus" });
  globalThis.reserved = [{ udid: "emulator-5556", device_type: "android_emulator" },
                         { udid: "emulator-5558", device_type: "android_emulator" }];
  result.first = JSON.parse(text(await tools.reserve_devices({ count: 2 })).text);
  globalThis.fixed = true;
  result.retry = JSON.parse(text(await tools.reserve_devices({ count: 2 })).text);
} else if (scenario === "carry-retry") {
  await tools.set_mock({ pattern: "~u /a", status_code: 200, body: "A" });
  result.switched = JSON.parse(text(await tools.use_remote({ name: "romulus" })).text);
  globalThis.mocksDown = true;
  globalThis.reserved = [{ udid: "SIM-R1", device_type: "simulator" }];
  result.down = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-R1"] })).text);
  globalThis.mocksDown = false;
  result.up = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-R1"] })).text);
  // Moved on again, the mock waits once more; clearing then drops it too.
  result.toDeimos = JSON.parse(text(await tools.use_remote({ name: "deimos", release: true })).text);
  await tools.clear_mocks({});
  result.afterClear = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-D1"] })).text);
} else if (scenario === "carry-hops") {
  // A: set 503+header, then update only the body: the server resets the rest.
  await tools.set_mock({ pattern: "~u /a", simulator_udid: "SIM-L", status_code: 503,
                         headers: { "retry-after": "5" }, body: "A" });
  await tools.update_mock({ rule_id: "mock_local1", body: "B" });
  // B: one the server says is already gone: clearing it forgets it.
  await tools.set_mock({ pattern: "~u /gone", simulator_udid: "SIM-L", status_code: 200, body: "G" });
  result.clearGone = text(await tools.clear_mocks({ rule_id: "mock_local2" }));
  await tools.use_remote({ name: "romulus" });
  // Two reservations at once, each of two simulators: each simulator gets the
  // mock once, not once per reservation.
  globalThis.reserved = [{ udid: "SIM-R1", device_type: "simulator" }, { udid: "SIM-R2", device_type: "simulator" }];
  const [one, two] = await Promise.all([tools.reserve_devices({ count: 2 }), tools.reserve_devices({ count: 2 })]);
  result.parallel = [JSON.parse(text(one).text).carried, JSON.parse(text(two).text).carried];
  // A second hop: one entry moves, not one per copy.
  result.toDeimos = JSON.parse(text(await tools.use_remote({ name: "deimos", release: true })).text);
  result.romulusPosts = romulus.bodies.filter((b) => b.path === "POST /api/v1/proxy/mocks").length;
}

if (scenario === "leave-unreachable" || scenario === "leave-erroring") {
  for (const p of ["/1", "/2"]) {
    await tools.set_mock({ pattern: `~u ${p}`, simulator_udid: "SIM-L", status_code: 200, body: p });
  }
  globalThis.hang = true;
  const t0 = Date.now();
  result.away = JSON.parse(text(await tools.use_remote({ name: "romulus", release: true })).text);
  result.seconds = (Date.now() - t0) / 1000;
} else if (scenario === "carry-stranded") {
  for (const p of ["/1", "/2", "/3"]) {
    await tools.set_mock({ pattern: `~u ${p}`, simulator_udid: "SIM-L", status_code: 200, body: p });
  }
  result.away = JSON.parse(text(await tools.use_remote({ name: "romulus" })).text);
  globalThis.back = true;
  result.back = JSON.parse(text(await tools.use_remote({ name: "local" })).text);
  result.again = JSON.parse(text(await tools.use_remote({ name: "romulus" })).text);
  result.backAgain = JSON.parse(text(await tools.use_remote({ name: "local" })).text);
  // mock_local3 still stands for mock /3 on SIM-L: reserving SIM-L sets up /1
  // and /2 there, but not /3 beneath it -- until it can be removed.
  globalThis.reserved = [{ udid: "SIM-L", device_type: "simulator" }];
  result.blocked = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-L"] })).text).carried;
  globalThis.local3ok = true;
  result.unblocked = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-L"] })).text).carried;
} else if (scenario === "carry-stranded-stall") {
  for (const p of ["/1", "/2", "/3"]) {
    await tools.set_mock({ pattern: `~u ${p}`, simulator_udid: "SIM-L", status_code: 200, body: p });
  }
  await tools.use_remote({ name: "romulus" });
  globalThis.back = true;
  const t0 = Date.now();
  result.back = JSON.parse(text(await tools.use_remote({ name: "local" })).text);
  result.seconds = (Date.now() - t0) / 1000;
} else if (scenario === "carry-ambiguous") {
  globalThis.made = [];
  await tools.set_mock({ pattern: "~u /amb", simulator_udid: "SIM-L", status_code: 200, body: "A" });
  await tools.use_remote({ name: "romulus" });
  globalThis.reserved = [{ udid: "SIM-R1", device_type: "simulator" }];
  result.first = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-R1"] })).text).carried;
  result.second = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-R1"] })).text).carried;
  result.made = globalThis.made.length;
} else if (scenario === "carry-race") {
  await tools.set_mock({ pattern: "~u /old", simulator_udid: "SIM-L", status_code: 200, body: "O" });
  await tools.add_session_capture({ simulator_udid: "SIM-L", processes: ["Metatext"] });
  await tools.add_session_capture({ simulator_udid: "SIM-L2", processes: ["Ivory", "Mona"] });
  await tools.add_session_capture({ simulator_udid: "SIM-L3", processes: ["Tusker"] });
  const switching = tools.use_remote({ name: "romulus" });
  await sleep(500);                       // switched, still deleting on local
  const late = tools.set_mock({ pattern: "~u /new", simulator_udid: "SIM-R9", status_code: 200, body: "N" });
  result.switched = JSON.parse(text(await switching).text);
  result.late = JSON.parse(text(await late).text);
  // Removed while waiting, by the simulator it was asked for on, or by some
  // of its processes there: not set up again (CodeRabbit).
  await tools.remove_session_capture({ simulator_udid: "SIM-L" });
  await tools.remove_session_capture({ simulator_udid: "SIM-L2", processes: ["Ivory"] });
  globalThis.reserved = [{ udid: "SIM-R1", device_type: "simulator" }];
  result.reserved = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-R1"] })).text);
  // An empty list removes all, as on the server: none moves at the next switch.
  await tools.remove_session_capture({ processes: [] });
  result.back = JSON.parse(text(await tools.use_remote({ name: "local", release: true })).text);
} else if (scenario === "carry-dead-old") {
  for (const p of ["/a", "/b", "/c"]) {
    await tools.set_mock({ pattern: `~u ${p}`, simulator_udid: "SIM-L", status_code: 200, body: p });
  }
  const t0 = Date.now();
  result.switched = JSON.parse(text(await tools.use_remote({ name: "romulus" })).text);
  result.seconds = (Date.now() - t0) / 1000;
} else if (scenario === "carry-partial") {
  await tools.set_mock({ pattern: "~u /p", simulator_udid: "SIM-L", status_code: 200, body: "P" });
  await tools.add_session_capture({ simulator_udid: "SIM-L", processes: ["Metatext"] });
  await tools.use_remote({ name: "romulus" });
  // Named simulators each time: a count asks the real server for others.
  globalThis.reserved = [{ udid: "SIM-R1", device_type: "simulator" }, { udid: "SIM-R2", device_type: "simulator" }];
  globalThis.failR2 = true;
  result.first = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-R1", "SIM-R2"] })).text).carried;
  // Capture stood on SIM-R1 and is waiting for SIM-R2; the agent removes it
  // from SIM-R1, then reserves SIM-R1 alone: nothing is put back there, and
  // what failed on SIM-R2 still waits (CodeRabbit).
  await tools.remove_session_capture({ simulator_udid: "SIM-R1" });
  // And clears the mock's rule on SIM-R1: not put back there either.
  await tools.clear_mocks({ rule_id: result.first.mocks_recreated[0].rule_id });
  globalThis.failR2 = false;
  globalThis.reserved = [{ udid: "SIM-R1", device_type: "simulator" }];
  result.aloneR1 = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-R1"] })).text).carried;
  globalThis.reserved = [{ udid: "SIM-R2", device_type: "simulator" }];
  result.second = JSON.parse(text(await tools.reserve_devices({ udids: ["SIM-R2"] })).text).carried;
  result.capturePosts = romulus.bodies.filter((b) => b.path === "POST /api/v1/proxy/session-capture")
    .map((b) => b.body.simulator_udid);
  result.posts = romulus.bodies.filter((b) => b.path === "POST /api/v1/proxy/mocks").map((b) => b.body.simulator_udid);
} else if (scenario === "carry-hops") {
  // A client key's release ends its scoped mocks on the server: not carried.
  await tools.use_remote({ name: "romulus" });
  globalThis.reserved = [{ udid: "SIM-R1", device_type: "simulator" }];
  await tools.reserve_devices({ udids: ["SIM-R1"] });
  globalThis.freed = ["SIM-R1"];
  await tools.release_devices({});
  result.afterRelease = JSON.parse(text(await tools.use_remote({ name: "local" })).text);
  result.mockBodies = romulus.bodies.filter((b) => b.path === "POST /api/v1/proxy/mocks").map((b) => b.body);
} else if (scenario === "hint") {
  writeFileSync(join(home, "quern"), `#!/bin/sh\ncat <<'J'\n${JSON.stringify({ remotes: [
    { name: "local", devices: [] },
    { name: "romulus", devices: [{ udid: "R1", name: "iPhone 15", model: "iPhone 15 Pro", os_version: "iOS 17.5",
      device_family: "iPhone", device_type: "simulator", free: true, state: "shutdown" }] },
  ] })}\nJ\n`);
  chmodSync(join(home, "quern"), 0o755);
  process.env.PATH = `${home}:/usr/bin:/bin`;
  globalThis.detail = "asked for 1 device(s), 0 free; 0 held by other sessions";
  result.noMatch = text(await tools.reserve_devices({ count: 1, os_version: "17.5" }));
  result.tooMany = text(await tools.reserve_devices({ count: 2, os_version: "17.5" }));
  result.bootedOnly = text(await tools.reserve_devices({ count: 1, os_version: "17.5", auto_boot: false }));
  result.listEmpty = JSON.parse(text(await tools.list_devices({ os_version: "17.5" })).text);
  result.listUnfiltered = JSON.parse(text(await tools.list_devices({})).text);
  result.listByState = JSON.parse(text(await tools.list_devices({ os_version: "17.5", state: "shutdown" })).text);
  result.listByModel = JSON.parse(text(await tools.list_devices({ name: "iPhone 15 Pro" })).text);
  result.listByName = JSON.parse(text(await tools.list_devices({ name: "Phone 15" })).text);
  globalThis.detail = "session agent-1 has no live connection";
  result.notLive = text(await tools.reserve_devices({ count: 1, os_version: "17.5" }));
}
result.current = current();
result.heldState = held.heldSessionState().state;
result.logs = { local: local.log, romulus: romulus.log, deimos: deimos.log };
result.bodies = { local: local.bodies, romulus: romulus.bodies };
console.log(JSON.stringify(result));
held.stopHeldSession();
setTimeout(() => process.exit(0), 100);
