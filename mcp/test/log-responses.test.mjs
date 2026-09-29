// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { tailLogsResult } from "../dist/tools/log-responses.js";

test("completeness fields are forwarded as the server sent them", () => {
  const out = tailLogsResult({
    entries: [{ message: "a" }],
    total: 999,
    has_more: true,
    truncated: true,
    complete_after: "2026-09-26T18:00:00Z",
  });
  assert.equal(out.truncated, true);
  assert.equal(out.complete_after, "2026-09-26T18:00:00Z");
  assert.equal(out.total, 1, "total means entries returned here");
});

test("a field the server adds later is not dropped", () => {
  const out = tailLogsResult({ entries: [], some_future_field: 7 });
  assert.equal(out.some_future_field, 7);
});

test("an older server's missing flag stays missing, never false", () => {
  // `false` is documented as a guarantee nothing was lost. An answer from a
  // server that cannot say must not be promoted to one.
  const out = tailLogsResult({ entries: [] });
  assert.equal("truncated" in out, false);
  assert.equal("complete_after" in out, false);
});

test("a false from the server is passed on as false", () => {
  assert.equal(tailLogsResult({ entries: [], truncated: false }).truncated, false);
});
