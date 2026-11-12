// Which mocks a switch carries after a lost session (#656): only those the
// server being left still holds for this opening of the session, or with none.
import { test } from "node:test";
import assert from "node:assert/strict";

import { stillOurs } from "../dist/carried.js";

const leaving = { id: "agent-1", opened: "2026-11-11T10:00:00+00:00" };

test("a rule this opening set, or one set with no session, still stands", () => {
  assert.equal(stillOurs({ rule_id: "m", session: "agent-1", session_opened: leaving.opened }, leaving), true);
  assert.equal(stillOurs({ rule_id: "m", session: null }, leaving), true);
});

test("a rule an earlier opening set ended with it, even before the server's clean-up", () => {
  assert.equal(stillOurs({ rule_id: "m", session: "agent-1",
                           session_opened: "2026-11-11T09:00:00+00:00" }, leaving), false);
});

test("a rule no longer listed has ended; another session's is not this agent's", () => {
  assert.equal(stillOurs(undefined, leaving), false);
  assert.equal(stillOurs({ rule_id: "m", session: "alice:agent-1" }, leaving), false);
});

test("a server too old to label rules is taken at its word", () => {
  assert.equal(stillOurs({ rule_id: "m" }, leaving), true);
  assert.equal(stillOurs({ rule_id: "m", session: "agent-1" }, leaving), true, "before #656: no opening");
});
