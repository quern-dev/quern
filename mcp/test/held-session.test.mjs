// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { sessionNotices, withNotices } from "../dist/http.js";

test("a pending session notice rides on the next object answer, once", () => {
  sessionNotices.push("reservations lost: reserve again");
  const first = withNotices({ status: "ok" });
  assert.deepEqual(first, { status: "ok", session_notices: ["reservations lost: reserve again"] });
  assert.deepEqual(withNotices({ status: "ok" }), { status: "ok" }, "delivered once, not repeated");
});

test("arrays, scalars and null carry no notices; they wait for an object", () => {
  sessionNotices.push("later");
  assert.deepEqual(withNotices([1, 2]), [1, 2]);
  assert.equal(withNotices(null), null);
  assert.equal(withNotices("x"), "x");
  assert.deepEqual(withNotices({}), { session_notices: ["later"] });
});
