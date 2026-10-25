#!/usr/bin/env node
"use strict";

// CommonJS launcher: this file must parse on ancient Node so we can
// surface a clear "Node too old" message when the MCP client launches
// us with the wrong binary (e.g. Claude Desktop picking nvm's lowest
// node off PATH). The ESM entry (dist/index.js) is loaded only after
// the version gate passes.

// The floor as [major, minor, patch]. Mirrors MIN_NODE in
// server/lifecycle/node_env.py and `engines` in package.json; a test keeps the
// three in step. 22.19, not 22: undici 8 declares >=22.19.0 (#506).
var REQUIRED = [22, 19, 0];
var REQUIRED_TEXT = REQUIRED[0] + "." + REQUIRED[1];
// ES5 on purpose -- this has to parse on the old node it is refusing.
var have = process.versions.node.split(".").map(function (n) { return parseInt(n, 10); });

function below(v, floor) {
  for (var i = 0; i < floor.length; i++) {
    var part = isNaN(v[i]) ? 0 : v[i];
    if (part !== floor[i]) { return part < floor[i]; }
  }
  return false;
}

if (isNaN(have[0]) || below(have, REQUIRED)) {
  process.stderr.write(
    "quern-debug-mcp: requires Node >= " + REQUIRED_TEXT +
    ", but is running on Node " + process.versions.node + "\n" +
    "  Binary: " + process.execPath + "\n" +
    "  Fix: in your MCP client config, set 'command' to an absolute path\n" +
    "       to a Node " + REQUIRED_TEXT + "+ binary, e.g.\n" +
    "       /Users/you/.nvm/versions/node/v" + REQUIRED[0] + ".x.x/bin/node\n"
  );
  process.exit(1);
}

// Each line's first release with the fix for CVE-2026-111497, the Brotli
// decoder flaw. Mirrors NODE_SECURITY_FLOORS in server/lifecycle/node_env.py;
// a test keeps the two in step. Warned about, never refused: the wrapper only
// talks to the local quern server, and refusing would take every quern tool
// away from a Node that runs it. A line not listed and older than the newest
// listed (23, 25, 20) has no fixed release.
var SECURITY = { 22: [22, 23, 4], 24: [24, 22, 1], 26: [26, 12, 1] };
var SECURITY_TEXT = "22.23.4+, 24.22.1+ or 26.12.1+";
var newestLine = 26;
var lineFloor = SECURITY[have[0]];
if (lineFloor ? below(have, lineFloor) : have[0] < newestLine) {
  process.stderr.write(
    "quern-debug-mcp: warning: Node " + process.versions.node + " lacks the fix for " +
    "CVE-2026-111497 (Brotli decoder).\n" +
    "  Binary: " + process.execPath + "\n" +
    "  Upgrade to Node " + SECURITY_TEXT + "; `quern doctor` shows the command.\n"
  );
}

var path = require("path");
var url = require("url");
var entryUrl = url.pathToFileURL(path.resolve(__dirname, "index.js")).href;

// Dynamic import() is a syntax error on Node < 12.17, so we hide it
// behind new Function — the body is parsed at Function() invocation
// time, after our version gate has already exited on those Nodes.
new Function("u", "return import(u)")(entryUrl).catch(function (err) {
  process.stderr.write(
    "quern-debug-mcp: failed to load server: " +
    ((err && err.stack) || err) + "\n"
  );
  process.exit(1);
});
