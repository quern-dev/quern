#!/usr/bin/env node
"use strict";

// CommonJS launcher: this file must parse on ancient Node so we can
// surface a clear "Node too old" message when the MCP client launches
// us with the wrong binary (e.g. Claude Desktop picking nvm's lowest
// node off PATH). The ESM entry (dist/index.js) is loaded only after
// the version gate passes.

var REQUIRED_MAJOR = 22;
var major = parseInt(process.versions.node.split(".")[0], 10);

if (isNaN(major) || major < REQUIRED_MAJOR) {
  process.stderr.write(
    "quern-debug-mcp: requires Node >= " + REQUIRED_MAJOR +
    ", but is running on Node " + process.versions.node + "\n" +
    "  Binary: " + process.execPath + "\n" +
    "  Fix: in your MCP client config, set 'command' to an absolute path\n" +
    "       to a Node " + REQUIRED_MAJOR + "+ binary, e.g.\n" +
    "       /Users/you/.nvm/versions/node/v" + REQUIRED_MAJOR + ".x.x/bin/node\n"
  );
  process.exit(1);
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
