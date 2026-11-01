// The device tools send the platform to the server (#582): one process, since
// the server URL and HOME are read at import. Prints one JSON line: what each
// tool sent. Driven by test/project-knowledge.test.mjs.
import { createServer } from "node:http";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [kind] = process.argv.slice(2);     // "ios-project" or "no-project"
const sent = [];
const srv = createServer(async (req, res) => {
  let raw = "";
  for await (const chunk of req) raw += chunk;
  const url = new URL(req.url, "http://x");
  sent.push({ path: `${req.method} ${url.pathname}`, query: Object.fromEntries(url.searchParams),
              body: raw ? JSON.parse(raw) : null });
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify(url.pathname.endsWith("/device/list")
    ? { devices: [{ udid: "x" }], tools: {}, active_udid: null } : { reserved: [] }));
});
await new Promise((ok) => srv.listen(0, "127.0.0.1", ok));

const home = mkdtempSync(join(tmpdir(), "quern-pw-"));
mkdirSync(join(home, ".quern"));
writeFileSync(join(home, ".quern", "api-key"), "k");
const project = mkdtempSync(join(tmpdir(), "quern-pw-proj-"));
if (kind === "ios-project") mkdirSync(join(project, "App.xcodeproj"));
process.env.HOME = home;
process.env.QUERN_STATE_DIR = join(home, ".quern");
process.env.QUERN_SERVER_URL = `http://127.0.0.1:${srv.address().port}`;
process.env.QUERN_PROJECT_DIR = project;

const tools = {};
const fake = { registerTool: (name, _def, handler) => { tools[name] = handler; } };
(await import("../dist/tools/device-pool.js")).registerDevicePoolTools(fake);
(await import("../dist/tools/device.js")).registerDeviceTools(fake);
(await import("../dist/tools/sessions.js")).registerSessionTools(fake);

const out = {};
for (const [name, args] of [
  ["resolve_device", { os_version: "16" }],
  ["ensure_devices", { count: 1, os_version: "16" }],
  ["list_devices", { os_version: "16" }],
  ["reserve_devices", { count: 1, os_version: "16" }],
  ["reserve_devices:asked", { count: 1, os_version: "16", platform: "android" }],
]) {
  sent.length = 0;
  const r = await tools[name.split(":")[0]](args);
  const hit = sent.find((s) => !s.path.startsWith("GET /health"));
  out[name] = { error: r.isError ? r.content[0].text : undefined, ...(hit ?? {}) };
}
console.log(JSON.stringify(out));
srv.close();
process.exit(0);
