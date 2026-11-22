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

// #756 review: the body match was a regex built from the listed body, with a
// lazy wildcard per placeholder -- slow on repeated fields, and loose enough
// for a placeholder to swallow fields beside it.

const json = (o) => ({ status_code: 200, headers: {}, body: JSON.stringify(o) });
const ph = (n) => `‹redacted, ${n} chars, id 0123456789ab›`;

test("a body placeholder stands for one value, not the fields beside it", () => {
  const listed = json({ password: ph(1), n: 1 });
  assert.ok(same(comparable(listed, json({ password: "x", n: 1 }))));
  assert.ok(same(comparable(listed, json({ password: 1234, n: 1 }))));      // a literal
  assert.ok(!same(comparable(listed, json({ password: "x", admin: true, n: 1 }))));
  assert.ok(!same(comparable(listed, json({ password: { a: 1 }, n: 1 }))));
  assert.ok(!same(comparable(listed, json({ password: "x", n: 2 }))));
});

test("a form body is matched pair by pair", () => {
  const listed = { status_code: 200, headers: {}, body: `user=me&password=${ph(7)}` };
  assert.ok(same(comparable(listed, { status_code: 200, headers: {}, body: "user=me&password=hunter2" })));
  assert.ok(!same(comparable(listed, { status_code: 200, headers: {}, body: "user=you&password=hunter2" })));
  assert.ok(!same(comparable(listed, { status_code: 200, headers: {}, body: "user=me&password=a&x=1" })));
});

test("many repeated redacted fields compare quickly and exactly", () => {
  const rows = Array.from({ length: 5000 }, () => ({ password: ph(1), x: "y" }));
  const listed = json(rows);
  const wanted = Array.from({ length: 5000 }, () => ({ password: "s", x: "y" }));
  wanted[4999].x = "z";                                    // differs only at the end
  const t0 = Date.now();
  assert.ok(!same(comparable(listed, json(wanted))));
  assert.ok(Date.now() - t0 < 500, `took ${Date.now() - t0} ms`);
});
