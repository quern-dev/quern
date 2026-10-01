// Runs against the compiled output: `npm run build && npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { plistValue } from "../dist/tools/app-state.js";

test("a boolean stays a boolean", () => {
  // It used to arrive at the server as 1 / 0 and be written as an integer.
  assert.equal(plistValue.parse(true), true);
  assert.equal(plistValue.parse(false), false);
});

test("numbers and strings pass through as themselves", () => {
  assert.equal(plistValue.parse(42), 42);
  assert.equal(plistValue.parse(3.5), 3.5);
  assert.equal(plistValue.parse("true"), "true");
  assert.equal(plistValue.parse("42"), "42");
});

test("anything else is refused rather than coerced", () => {
  assert.equal(plistValue.safeParse(null).success, false);
  assert.equal(plistValue.safeParse({}).success, false);
});
