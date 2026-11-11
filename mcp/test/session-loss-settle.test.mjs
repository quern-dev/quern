// Reconciling what a lost session (#650) took with it, against the server's listings.
import { test } from "node:test";
import assert from "node:assert/strict";

import { recordMock, resetCarried, trackedMockRules } from "../dist/carried.js";
import { noteLoss, settleLoss } from "../dist/tools/intercept.js";

const mock = (p) => ({ pattern: p, response: { status_code: 200 }, simulator_udid: null });
const listing = (rules) => async () => [rules, []];

test("only rules tracked at the loss are judged; one set meanwhile is kept", async () => {
  resetCarried();
  recordMock("m_lost", mock("~u /a"));
  recordMock("m_gap", mock("~u /b"));
  noteLoss("agent-1");
  recordMock("m_new", mock("~u /c"));         // set while the listings are read
  await settleLoss({ read: listing([{ rule_id: "m_lost", session: "agent-1" },
                                    { rule_id: "m_gap", session: null }]) });
  assert.deepEqual(trackedMockRules().sort(), ["m_gap", "m_new"]);
  resetCarried();
});

test("an unreadable server leaves the loss pending; a later settle applies it", async () => {
  resetCarried();
  recordMock("m_lost", mock("~u /a"));
  noteLoss("agent-1");
  await settleLoss({ read: async () => { throw new Error("HTTP 503"); } });
  assert.deepEqual(trackedMockRules(), ["m_lost"], "could not tell: nothing dropped");
  await settleLoss({ read: listing([{ rule_id: "m_lost", session: "agent-1" }]) });
  assert.deepEqual(trackedMockRules(), []);
  resetCarried();
});

test("before use_remote, an unreadable server drops the candidates, not later rules", async () => {
  resetCarried();
  recordMock("m_lost", mock("~u /a"));
  noteLoss("agent-1");
  recordMock("m_new", mock("~u /c"));
  await settleLoss({ orDrop: true, read: async () => { throw new Error("unreachable"); } });
  assert.deepEqual(trackedMockRules(), ["m_new"]);
  resetCarried();
});
