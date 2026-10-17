#!/usr/bin/env node
// A/B harness for the viewer's MSE muxer (#449): our server/device/media/web/
// fmp4.js against jmuxer, playing the same live quern-media stream.
//
//   node scripts/fmp4-ab.mjs --frames http://127.0.0.1:8490/frames \
//        [--jmuxer /path/to/jmuxer.min.js] [--port 9191] [--bind 127.0.0.1]
//
// Open http://<bind>:<port>/?muxer=quern or ?muxer=jmuxer. Each page reports
// what it measured every two seconds -- frames in, frames presented, buffered
// lag behind live, errors -- as one JSON line on this script's stdout.
//
// jmuxer comes only from --jmuxer, a local copy: nothing in the repository or
// a release depends on it, by design. Without it, ?muxer=jmuxer says so.
//
// Bind to a LAN address to test a browser on another machine; the page is then
// a non-secure origin, which is the case MSE is for (Chrome hides WebCodecs
// there). Nothing here is authenticated: it is a measurement tool, so only run
// it on a network you trust, and stop it when done.
import { createReadStream } from "node:fs";
import http from "node:http";
import { fileURLToPath } from "node:url";

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) =>
  (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc), []));
if (!args.frames) {
  console.error("usage: fmp4-ab.mjs --frames <quern-media /frames URL> [--jmuxer path] [--port n] [--bind addr]");
  process.exit(2);
}
const port = Number(args.port || 9191);
const bind = args.bind || "127.0.0.1";
const muxerPath = fileURLToPath(new URL("../server/device/media/web/fmp4.js", import.meta.url));
const pagePath = fileURLToPath(new URL("./fmp4-ab.html", import.meta.url));

http.createServer((req, res) => {
  const path = req.url.split("?")[0];
  const send = (type, file) => {
    res.writeHead(200, { "Content-Type": type, "Cache-Control": "no-store" });
    createReadStream(file).pipe(res);
  };
  if (path === "/") return send("text/html", pagePath);
  if (path === "/fmp4.js") return send("text/javascript", muxerPath);
  if (path === "/jmuxer.js") {
    if (!args.jmuxer) { res.writeHead(404); return res.end("start the harness with --jmuxer"); }
    return send("text/javascript", args.jmuxer);
  }
  if (path === "/frames") {
    const up = http.get(args.frames, (r) => {
      res.writeHead(r.statusCode, { "Content-Type": "application/octet-stream", "Cache-Control": "no-store" });
      r.pipe(res);
    });
    up.on("error", (e) => { res.writeHead(502); res.end(String(e)); });
    req.on("close", () => up.destroy());
    return undefined;
  }
  if (path === "/report" && req.method === "POST") {
    let body = "";
    req.on("data", (c) => { body += c; });
    req.on("end", () => {
      try {
        process.stdout.write(`${JSON.stringify({ t: new Date().toISOString(), from: req.socket.remoteAddress, ...JSON.parse(body) })}\n`);
      } catch { /* a malformed report is dropped */ }
      res.writeHead(204);
      res.end();
    });
    return undefined;
  }
  res.writeHead(404);
  return res.end();
}).listen(port, bind, () => console.error(`fmp4 A/B harness on http://${bind}:${port}/  (stream ${args.frames})`));
