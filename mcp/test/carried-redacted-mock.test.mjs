// Runs against the compiled output: `npm run build && npm test`.
//
// #742 review: a mock listing redacts secret response headers, so adoption
// after a failed create compared a placeholder with the real value, never
// matched, and posted a second rule.
import assert from "node:assert/strict";
import { test } from "node:test";

import { comparable } from "../dist/carried.js";

const P = "sid=‹redacted, 6 chars, id 0123456789ab›; Path=/";
const same = ([a, b]) => a === b;

test("a header listed redacted matches on its name", () => {
  const listed = { status_code: 200, headers: { "Set-Cookie": P }, body: "" };
  const wanted = { status_code: 200, headers: { "Set-Cookie": "sid=abc123; Path=/" }, body: "" };
  assert.ok(same(comparable(listed, wanted)));
});

test("everything else is still compared", () => {
  const listed = { status_code: 200, headers: { "Set-Cookie": P }, body: "" };
  assert.ok(!same(comparable(listed, { status_code: 500, headers: { "Set-Cookie": "x" }, body: "" })));
  assert.ok(!same(comparable(listed, { status_code: 200, headers: {}, body: "" })));
  assert.ok(!same(comparable({ status_code: 200, headers: { "X-A": "1" } },
    { status_code: 200, headers: { "X-A": "2" } })));
});
