// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { guidePages } from "../dist/guide-pages.js";

test("each contents line names one page and what it answers", () => {
  const contents = [
    "# Network",
    "",
    "- `quern://guide/network/mocks` -- Mocking: set_mock and update_mock",
    "- `quern://guide/network/slow-network` -- Slowing the network: intercept holds",
    "- `quern://guide/ui/reading` -- another topic's page is not this topic's",
    "Prose mentioning `quern://guide/network/capture` is not a contents line.",
  ].join("\n");
  assert.deepEqual(guidePages("network", contents), [
    ["mocks", "Mocking: set_mock and update_mock"],
    ["slow-network", "Slowing the network: intercept holds"],
  ]);
});

test("the real contents pages each name pages", () => {
  // The registration loop reads these files; a format drift would register nothing.
  for (const topic of ["ui", "network", "devices", "logs", "performance", "sharing"]) {
    const text = readFileSync(new URL(`../../docs/agent-guide/${topic}.md`, import.meta.url), "utf-8");
    assert.ok(guidePages(topic, text).length >= 2, `${topic}: no pages parsed`);
  }
});
