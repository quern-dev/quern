// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { pendingUpgradeFieldsFrom } from "../dist/tools/logs.js";

const pending = [{ name: "pymobiledevice3", current: "11.14.0", floor: "11.55.0",
                   command: ["sudo", "pip", "install", "-U", "pymobiledevice3"],
                   needs_root: true }];

test("a pending upgrade is carried with a note to run it in a terminal (#725)", () => {
  const got = pendingUpgradeFieldsFrom({ pending_tool_upgrades: pending });
  assert.deepEqual(got.pending_tool_upgrades, pending);
  assert.match(got.pending_tool_upgrades_note, /quern update` in a terminal/);
});

test("a remote's note sends the user to that machine", () => {
  const got = pendingUpgradeFieldsFrom({ pending_tool_upgrades: pending }, true);
  assert.match(got.pending_tool_upgrades_note, /on that machine/);
});

test("nothing pending, or an older server, adds no fields", () => {
  assert.deepEqual(pendingUpgradeFieldsFrom({ pending_tool_upgrades: [] }), {});
  assert.deepEqual(pendingUpgradeFieldsFrom({ update_available: false }), {});
  assert.deepEqual(pendingUpgradeFieldsFrom(null), {});
  assert.deepEqual(pendingUpgradeFieldsFrom({ pending_tool_upgrades: "x" }), {});
});
