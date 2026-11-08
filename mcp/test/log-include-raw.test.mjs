// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { includeRaw } from "../dist/tools/logs.js";

test("the string false means false", () => {
  // z.coerce.boolean() made it true, sending the field it exists to omit (#630).
  assert.equal(includeRaw.parse("false"), false);
  assert.equal(includeRaw.parse("true"), true);
});

test("booleans and absence pass through", () => {
  assert.equal(includeRaw.parse(true), true);
  assert.equal(includeRaw.parse(false), false);
  assert.equal(includeRaw.parse(undefined), undefined);
});

test("anything else is refused rather than coerced", () => {
  assert.equal(includeRaw.safeParse("yes").success, false);
  assert.equal(includeRaw.safeParse(1).success, false);
});
