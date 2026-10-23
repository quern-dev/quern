import { spawn, type ChildProcess } from "node:child_process";
import { createServer, request as httpRequest, type Server } from "node:http";
import { homedir } from "node:os";
import { join } from "node:path";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams, errorText } from "./helpers.js";

// SPIKE: a live simulator preview as an MCP App view (MCP Apps extension,
// `text/html;profile=mcp-app`). Measured so far in Claude Desktop 3.6556:
// the view renders inline; the host offers inline and fullscreen, not pip;
// a fullscreen request from the view is answered "inline"; and an <img> of
// the http://127.0.0.1 MJPEG stream did not load despite the declared CSP.
// This version reports why, trying the stream several ways.

const VIEW_URI = "ui://quern/preview";
const MIME = "text/html;profile=mcp-app";

async function streamOrigins(): Promise<string[]> {
  const origins = ["http://127.0.0.1:*", "http://localhost:*"];
  try {
    const status = (await apiRequest("GET", "/api/v1/device/preview/status")) as {
      ios?: { active?: Record<string, { stream_port?: number }> };
    };
    for (const p of Object.values(status.ios?.active ?? {})) {
      if (p.stream_port) origins.push(`http://127.0.0.1:${p.stream_port}`);
    }
    origins.push(`http://127.0.0.1:${await streamFront()}`);
  } catch {
    // The wildcard alone, then.
  }
  return origins;
}

// -- a CORS front for the stream ----------------------------------------------
//
// quern-media sends no cross-origin headers, so a view on another origin
// cannot read its stream with fetch(); and Chrome's local-network rules want
// Access-Control-Allow-Private-Network on loopback. This passes the stream
// through untouched with both. If the spike works, quern-media sends them.

let front: { server: Server; port: number } | null = null;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Private-Network": "true",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "*",
};

async function streamFront(): Promise<number> {
  if (front) return front.port;
  const server = createServer((req, res) => {
    if (req.method === "OPTIONS") { res.writeHead(204, CORS); res.end(); return; }
    const url = new URL(req.url ?? "/", "http://x");
    const port = Number(url.searchParams.get("port"));
    url.searchParams.delete("port");
    const upstream = httpRequest(
      { host: "127.0.0.1", port, path: url.pathname + url.search, method: "GET" },
      (up) => {
        // Labelled for what it is, so a browser plays it natively too.
        const headers = { ...up.headers, ...CORS };
        if (url.pathname === "/frames") headers["content-type"] = "multipart/x-mixed-replace; boundary=quernframe";
        res.writeHead(up.statusCode ?? 502, headers);
        up.pipe(res);
      },
    );
    upstream.on("error", (e) => { res.writeHead(502, CORS); res.end(String(e)); });
    req.on("close", () => upstream.destroy());
    upstream.end();
  });
  const port = await new Promise<number>((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve((server.address() as { port: number }).port));
  });
  front = { server, port };
  return port;
}

// -- a stream sized for a panel --------------------------------------------------
//
// The preview window's stream is native resolution (2854x2008 on a Duo,
// ~570KB a frame): decoding that in the view held it under 4fps. The panel
// gets its own quern-media, sized for a panel.

const panelStreams = new Map<string, { child: ChildProcess; port: number }>();

function mediaBinary(): string {
  return join(process.env.QUERN_STATE_DIR || join(homedir(), ".quern"), "bin", "quern-media");
}

async function panelStream(udid: string): Promise<number> {
  const live = panelStreams.get(udid);
  if (live && live.child.exitCode === null) return live.port;
  const port = await new Promise<number>((resolve) => {
    const probe = createServer();
    probe.listen(0, "127.0.0.1", () => {
      const p = (probe.address() as { port: number }).port;
      probe.close(() => resolve(p));
    });
  });
  const child = spawn(mediaBinary(), [
    "--sim-udid", udid, "--serve", String(port),
    "--max-dim", "1000", "--quality", "0.6", "--fps", "30", "--exit-with-parent",
  ], { stdio: "ignore" });
  panelStreams.set(udid, { child, port });
  // Serving within a few seconds; the view retries its fetch until then.
  for (let i = 0; i < 40; i++) {
    const up = await new Promise<boolean>((resolve) => {
      const r = httpRequest({ host: "127.0.0.1", port, path: "/", method: "HEAD" }, () => resolve(true));
      r.on("error", () => resolve(false));
      r.end();
    });
    if (up) break;
    await new Promise((z) => setTimeout(z, 100));
  }
  return port;
}

process.on("exit", () => { for (const s of panelStreams.values()) s.child.kill(); });

// -- the view --------------------------------------------------------------------

const VIEW_HTML = `<!doctype html>
<html><head><meta charset="utf-8">
<style>
  html, body { margin: 0; background: #111; color: #ccc; font: 12px -apple-system, sans-serif; }
  #bar { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; padding: 6px; }
  button { font: inherit; padding: 3px 8px; border-radius: 5px; border: 1px solid #444; background: #222; color: #ddd; cursor: pointer; }
  button:hover:not(:disabled) { background: #2d2d2d; }
  button:active:not(:disabled) { background: #3a3a3a; }
  button:disabled { opacity: 0.4; cursor: default; }
  button.working { opacity: 1; border-color: #6a8; color: #fff; }
  .seg button.on:disabled { opacity: 1; cursor: default; }
  #out { padding: 0 6px 6px; min-height: 1.3em; color: #9c9; }
  .seg { display: inline-flex; }
  .seg button { border-radius: 0; margin-left: -1px; }
  .seg button:first-child { border-radius: 5px 0 0 5px; margin-left: 0; }
  .seg button:last-child { border-radius: 0 5px 5px 0; }
  .seg button.on { background: #35553f; border-color: #6a8; color: #fff; opacity: 1; }
  #out.err { color: #e88; }
  #v { display: block; width: 100%; height: auto; min-height: 240px; background: #000; }
  /* In the side pane the host fixes the height: the video takes whatever
     the controls leave, letterboxed, instead of leaving it empty below. */
  body.pane, body.pane #root { height: 100vh; }
  body.pane #root { display: flex; flex-direction: column; }
  body.pane #v { flex: 1 1 auto; min-height: 0; height: auto; width: 100%; object-fit: contain; }
  #info { padding: 4px 6px; color: #888; white-space: pre-wrap; word-break: break-all; }
  #info[hidden] { display: none; }
  #details { margin-left: auto; color: #999; }
  #details.on { color: #ddd; border-color: #666; }
</style></head>
<body>
<div id="root">
<div id="bar">
  <span id="st">connecting…</span>
  <button id="fold">Fold</button>
  <button id="rotate" data-doing="Rotating…">Rotate ↻</button>
  <span class="seg">
    <button data-orient="portrait" data-shape="landscape" data-doing="Landscape…" title="Device Hub: portrait">Landscape</button>
    <button data-orient="landscape-left" data-shape="portrait" data-doing="Portrait…" title="Device Hub: landscape left">Portrait</button>
  </span>
  <button id="mode">Side Pane</button>
  <button id="details" title="What the view and Claude reported">Details</button>
</div>
<div id="out"></div>
<canvas id="v"></canvas>
<div id="info" hidden></div>
</div>
<script>
  const st = document.getElementById("st"), v = document.getElementById("v"), info = document.getElementById("info");
  let nextId = 0, udid = null, ready = false, name = ""; const pending = {};
  const log = (s) => { info.textContent = (info.textContent + "\\n" + s).trim().split("\\n").slice(-14).join("\\n"); };
  document.addEventListener("securitypolicyviolation", (e) => {
    log("CSP blocked " + e.blockedURI + " by " + e.effectiveDirective + " | policy: " + String(e.originalPolicy).slice(0, 300));
  });
  // A request the host never answers must not leave the view waiting.
  function request(method, params, timeoutMs = 20000) {
    const id = ++nextId;
    parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
    return new Promise((res, rej) => {
      const t = setTimeout(() => { delete pending[id]; rej({ message: "no answer from Claude in " + timeoutMs / 1000 + "s" }); }, timeoutMs);
      pending[id] = { res: (r) => { clearTimeout(t); res(r); }, rej: (e) => { clearTimeout(t); rej(e); } };
    });
  }
  function notify(method, params) { parent.postMessage({ jsonrpc: "2.0", method, params }, "*"); }
  function sendSize() {
    if (!ready) return;
    // The content's own height, not the document's: a document is never
    // shorter than the frame it is in, so hiding the details left their space.
    const r = document.getElementById("root").getBoundingClientRect();
    notify("ui/notifications/size-changed", { width: Math.ceil(r.width), height: Math.ceil(r.height) });
  }
  window.addEventListener("message", (e) => {
    const m = e.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.id !== undefined && pending[m.id]) {
      const p = pending[m.id]; delete pending[m.id];
      m.error ? p.rej(m.error) : p.res(m.result);
      return;
    }
    if (m.method === "ui/notifications/tool-result") show(m.params);
    else if (m.method === "ui/notifications/host-context-changed") {
      if (m.params.displayMode) setMode(m.params.displayMode);
      log("host context changed: " + JSON.stringify({ mode: m.params.displayMode, dims: m.params.containerDimensions }));
    }
  });
  function show(result) {
    const s = (result && result.structuredContent) || {};
    if (!s.url) { st.textContent = "no stream"; log(JSON.stringify(result && result.content)); return; }
    udid = s.udid; name = s.name;
    readHinge();
    log("view origin: " + location.origin + " | secure: " + window.isSecureContext);
    play(s.url);
  }
  // Reads the stream with fetch() and draws each JPEG on the canvas: no
  // img-src involved, only connect-src.
  async function play(url) {
    st.textContent = name + " — connecting to " + url;
    let res;
    try { res = await fetch(url); }
    catch (e) { st.textContent = name + " — fetch failed"; log("fetch failed: " + e.name + ": " + e.message); sendSize(); return; }
    log("stream: HTTP " + res.status + " " + res.headers.get("content-type"));
    const reader = res.body.getReader(), ctx = v.getContext("2d");
    let buf = new Uint8Array(0), frames = 0, received = 0; const drawnAt = [];
    const dec = new TextDecoder("latin1");
    for (;;) {
      const { value, done } = await reader.read();
      if (done) { log("stream ended"); return; }
      const joined = new Uint8Array(buf.length + value.length); joined.set(buf); joined.set(value, buf.length); buf = joined;
      for (;;) {
        // Headers end at CRLFCRLF; Content-Length says how much JPEG follows.
        let h = -1;
        for (let i = 0; i + 3 < buf.length && i < 512; i++) { if (buf[i] === 13 && buf[i+1] === 10 && buf[i+2] === 13 && buf[i+3] === 10) { h = i; break; } }
        if (h < 0) break;
        const m = /Content-Length:\\s*(\\d+)/i.exec(dec.decode(buf.subarray(0, h)));
        if (!m) { buf = buf.subarray(h + 4); continue; }
        const len = +m[1], start = h + 4;
        if (buf.length < start + len) break;
        const jpeg = buf.slice(start, start + len);
        buf = buf.subarray(start + len);
        received += 1;
        // Skip a frame if one is still decoding: newest wins.
        if (play.busy) continue;
        play.busy = true;
        createImageBitmap(new Blob([jpeg], { type: "image/jpeg" })).then((bmp) => {
          if (v.width !== bmp.width || v.height !== bmp.height) { v.width = bmp.width; v.height = bmp.height; sendSize(); }
          const w = bmp.width, h = bmp.height;
          ctx.drawImage(bmp, 0, 0); bmp.close(); play.busy = false;
          frames += 1;
          // Frames drawn in the last second: an idle screen sends none, and
          // an average since opening counted that as slowness.
          const now = performance.now(); drawnAt.push(now);
          while (drawnAt.length && now - drawnAt[0] > 1000) drawnAt.shift();
          st.textContent = name + " — live, " + drawnAt.length + " fps (" + w + "x" + h + ", " + Math.round(jpeg.length / 1024) + " KB, " + (received - frames) + " skipped)";
        }).catch((e) => { play.busy = false; log("decode failed: " + e.message); });
      }
    }
  }
  // -- device buttons: one action at a time, each says how it ended ------------
  const out = document.getElementById("out");
  const foldBtn = document.getElementById("fold");
  const actions = [...document.querySelectorAll("button[data-orient], #rotate"), foldBtn];
  const rotateBtn = document.getElementById("rotate");
  // Device Hub's picker names, which say how the device is held, in the
  // order Rotate steps through them: a quarter turn each, the same way. The
  // buttons are labelled by what the screen shows instead -- the inner
  // panel held "portrait" shows a landscape screen. uiOrientation each
  // gives, measured on a Duo: portrait 3, landscape-left 1, pud 4,
  // landscape-right 2.
  const CYCLE = ["portrait", "landscape-left", "pud", "landscape-right"];
  const ORIENT_OF = { 3: "portrait", 2: "landscape-right", 4: "pud", 1: "landscape-left" };
  let busy = false, folded = null, orient = null;   // null until the hinge is read
  const shapeOf = (o) => o === "portrait" || o === "pud" ? "landscape" : o ? "portrait" : null;
  function say(text, err) { out.textContent = text; out.className = err ? "err" : ""; sendSize(); }
  // Enabled only where the action makes sense: no folding a folded device,
  // turning only when unfolded (the picker turns the inner panel).
  function refresh() {
    // One button for the hinge, saying what it will do. Before the hinge is
    // read it offers Fold, which every pose answers sensibly.
    if (!foldBtn.classList.contains("working")) foldBtn.textContent = folded ? "Unfold" : "Fold";
    for (const b of actions) {
      // Turning only when unfolded: the picker turns the inner panel.
      const sensible = b === foldBtn || folded === null || !folded;
      // The shape already showing is not an action: pressed, it waited ~4s
      // for a change that never came.
      const already = !!b.dataset.shape && !folded && b.dataset.shape === shapeOf(orient);
      b.disabled = busy || !udid || !sensible || already;
      // The picture is always shown upright, so held upside down looks the
      // same as held the right way: the selector says landscape or portrait.
      if (b.dataset.shape) b.classList.toggle("on", !folded && b.dataset.shape === shapeOf(orient));
    }
  }
  function learn(r) {
    // The tool answers JSON as text; lit_panel says which panel lit.
    try {
      const d = JSON.parse(((r && r.content) || [])[0].text);
      if (d.lit_panel) {
        folded = d.lit_panel.device_name === "primary";
        orient = folded ? null : (ORIENT_OF[d.lit_panel.orientation] || null);
      }
      return d;
    } catch (e) { return null; }
  }
  async function act(b, args) {
    if (busy) return;
    busy = true; refresh();
    const label = b.textContent, doing = b === foldBtn ? label + "ing…" : b.dataset.doing;   // Folding…, Unfolding…
    b.classList.add("working"); b.textContent = doing;
    say(doing);
    const t = performance.now();
    try {
      const r = await request("tools/call", { name: "set_hinge", arguments: Object.assign({ udid }, args) });
      const secs = ((performance.now() - t) / 1000).toFixed(1);
      if (r && r.isError) say(label + " failed after " + secs + "s: " + (((r.content || [])[0] || {}).text || "").slice(0, 200), true);
      else {
        const d = learn(r);
        const lit = d && d.lit_panel ? (d.lit_panel.device_name === "primary" ? "cover" : "inner panel") + " lit" : "done";
        say(label + ": " + lit + ", " + secs + "s" + (d && d.warning ? " — " + d.warning : ""), !!(d && d.warning));
      }
    } catch (e) {
      say(label + " failed: " + (e.message || JSON.stringify(e)), true);
    } finally {
      b.classList.remove("working"); if (b !== foldBtn) b.textContent = label;
      busy = false; refresh();
    }
  }
  foldBtn.onclick = () => act(foldBtn, { pose: folded ? "open" : "closed" });
  actions.forEach((b) => {
    if (b === rotateBtn || b === foldBtn) return;
    b.onclick = () => act(b, b.dataset.pose ? { pose: b.dataset.pose } : { orientation: b.dataset.orient });
  });
  // The next position after the current one; from an unknown one, portrait.
  rotateBtn.onclick = () => {
    const i = CYCLE.indexOf(orient);
    act(rotateBtn, { orientation: CYCLE[(i + 1) % CYCLE.length] });
  };
  async function readHinge() {
    try { learn(await request("tools/call", { name: "get_hinge", arguments: { udid } })); }
    catch (e) { log("get_hinge failed: " + (e.message || JSON.stringify(e))); }
    refresh();
  }
  refresh();
  // One button for where the view is shown, saying where it will go. The
  // host has the last word -- Claude Desktop kept a view's own request for
  // fullscreen inline, and moves it to the side panel from its ⤢ -- so the
  // label follows what the host reports, not what was asked.
  const modeBtn = document.getElementById("mode");
  let mode = "inline";
  // Claude Desktop shows a view's "fullscreen" as its side pane.
  function setMode(m) {
    mode = m;
    modeBtn.textContent = m === "fullscreen" ? "Inline" : "Side Pane";
    document.body.classList.toggle("pane", m === "fullscreen");
    sendSize();
  }
  modeBtn.onclick = async () => {
    const want = mode === "fullscreen" ? "inline" : "fullscreen";
    modeBtn.disabled = true;
    try {
      const r = await request("ui/request-display-mode", { mode: want });
      const got = (r && r.mode) || mode;
      setMode(got);
      if (got !== want) say("Claude kept the view " + (got === "fullscreen" ? "in the side pane" : "inline") + "; use ⤢ to move it.", true);
      else say(got === "fullscreen" ? "In the side pane." : "Inline.");
    } catch (err) { say("Could not change where the view is shown: " + (err.message || JSON.stringify(err)), true); }
    finally { modeBtn.disabled = false; sendSize(); }
  };
  // Diagnostics stay collected while hidden, so opening them after a
  // problem shows what led up to it.
  const detailsBtn = document.getElementById("details");
  detailsBtn.onclick = () => {
    info.hidden = !info.hidden;
    detailsBtn.classList.toggle("on", !info.hidden);
    sendSize();
  };
  new ResizeObserver(sendSize).observe(document.body);
  request("ui/initialize", {
    appInfo: { name: "quern preview", version: "0.1.5" },
    appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
    protocolVersion: "2026-01-26",
  }).then((r) => {
    const c = (r && r.hostContext) || {};
    log("host: " + JSON.stringify({ mode: c.displayMode, modes: c.availableDisplayModes, dims: c.containerDimensions }));
    if (c.displayMode) setMode(c.displayMode);
    notify("ui/notifications/initialized", {});
    ready = true;
    sendSize();
  }).catch((err) => { st.textContent = "initialize failed"; log("initialize failed: " + JSON.stringify(err)); });
</script>
</body></html>`;

export function registerPreviewAppTools(server: McpServer): void {
  server.registerResource(
    "preview-view",
    VIEW_URI,
    { description: "SPIKE: a live simulator preview, as an MCP App view", mimeType: MIME },
    async () => {
      const origins = await streamOrigins();
      return {
        contents: [{
          uri: VIEW_URI,
          mimeType: MIME,
          text: VIEW_HTML,
          _meta: { ui: { csp: { resourceDomains: origins, connectDomains: origins }, prefersBorder: true } },
        }],
      };
    },
  );

  server.registerTool("show_preview", {
    description: `SPIKE: show a booted iOS simulator's live screen in the conversation, as an MCP App view, with fold and rotate buttons.`,
    inputSchema: strictParams({
      udid: z.string().describe("A booted simulator's UDID"),
    }),
    _meta: { ui: { resourceUri: VIEW_URI }, "ui/resourceUri": VIEW_URI },
  }, async ({ udid }) => {
    try {
      type Status = { ios?: { active?: Record<string, { stream_port?: number; name?: string }> } };
      const status = (await apiRequest("GET", "/api/v1/device/preview/status")) as Status;
      const entry = Object.entries(status.ios?.active ?? {}).find(([k]) => k === udid)?.[1];
      const url = `http://127.0.0.1:${await streamFront()}/frames?source=${udid}&port=${await panelStream(udid)}`;
      return {
        content: [{ type: "text" as const, text: `Showing ${entry?.name ?? udid} live.` }],
        structuredContent: { udid, name: entry?.name ?? udid, ...(url ? { url } : {}) },
      };
    } catch (e) {
      return { content: [{ type: "text" as const, text: errorText(e) }], isError: true };
    }
  });
}
