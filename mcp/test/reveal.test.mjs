// Runs against the compiled output: `npm run build && npm test`.
//
// #742: the server reads a bare "true" as "reveal every secret". A list of
// names must never reach it as that, or asking for one parameter named `true`
// would show every other secret (review).
import assert from "node:assert/strict";
import { test } from "node:test";

import { revealQuery } from "../dist/tools/helpers.js";

test("true reveals everything, and false or nothing reveals nothing", () => {
  assert.equal(revealQuery(true), "true");
  assert.equal(revealQuery(false), undefined);
  assert.equal(revealQuery(undefined), undefined);
  assert.equal(revealQuery([]), undefined);
});

test("a list of names never reads as reveal-all", () => {
  assert.notEqual(revealQuery(["true"]), "true");
  assert.equal(revealQuery(["true"]), "true,");
  assert.equal(revealQuery(["authorization", "cookie"]), "authorization,cookie,");
});
