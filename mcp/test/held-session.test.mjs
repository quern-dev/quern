// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { sessionNotices } from "../dist/http.js";
import { withSessionNotices } from "../dist/notices.js";

test("a pending notice is appended to the next tool result, once", () => {
  sessionNotices.push("reservations lost: reserve again");
  const result = { content: [{ type: "text", text: '{"summary":"ok"}' }] };
  const out = withSessionNotices(result);
  assert.equal(out.content.length, 2);
  assert.equal(out.content[0].text, '{"summary":"ok"}', "the tool's own output is untouched");
  assert.match(out.content[1].text, /quern session notice: reservations lost/);
  assert.deepEqual(withSessionNotices(result), result, "delivered once, not repeated");
});

test("it survives whatever the tool did with the API's JSON, errors included", () => {
  sessionNotices.push("later");
  const error = { content: [{ type: "text", text: "Error: HTTP 500" }], isError: true };
  const out = withSessionNotices(error);
  assert.equal(out.isError, true);
  assert.match(out.content[1].text, /later/);
});

test("nothing pending, nothing added", () => {
  const result = { content: [{ type: "text", text: "x" }] };
  assert.equal(withSessionNotices(result), result);
});
