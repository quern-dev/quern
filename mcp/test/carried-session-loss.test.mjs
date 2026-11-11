// After a lost session (#650): which tracked mocks the server ended with it.
import { test } from "node:test";
import assert from "node:assert/strict";

import { endedWith, forgetEndedMocks, recordMock, resetCarried, trackedMockRules } from "../dist/carried.js";

test("a rule listed under the lost session, or not listed, ended; one with no session stays", () => {
  const ended = endedWith("agent-1", [
    { rule_id: "m_lost", session: "agent-1" },
    { rule_id: "m_gap", session: null },        // set while the session was gone
    { rule_id: "m_other", session: "alice:agent-1" },
  ]);
  assert.equal(ended("m_lost"), true);
  assert.equal(ended("m_gone"), true, "not listed: the clean-up already ran");
  assert.equal(ended("m_gap"), false);
  assert.equal(ended("m_other"), false, "another key's session of the same name");
});

test("forgetting the ended ones keeps the rest tracked", () => {
  resetCarried();
  const mock = (p) => ({ pattern: p, response: { status_code: 200 }, simulator_udid: null });
  recordMock("m_lost", mock("~u /a"));
  recordMock("m_gap", mock("~u /b"));
  forgetEndedMocks(endedWith("agent-1", [{ rule_id: "m_lost", session: "agent-1" },
                                         { rule_id: "m_gap", session: null }]));
  assert.deepEqual(trackedMockRules(), ["m_gap"]);
  resetCarried();
});
