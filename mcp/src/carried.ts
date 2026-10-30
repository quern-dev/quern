/** What this agent set up on its server that follows it to another (use_remote).
 *
 * The server cannot say which mocks are this agent's: it records an owner per
 * key, and every MCP on the server's own Mac uses the same owner key. So this
 * MCP keeps its own record of the mocks and session capture it set, as it set
 * them -- a client key's scoped mocks are deleted by the server when its
 * device is released, which a switch requires, so they cannot be read back
 * then.
 *
 * One entry per mock the agent set, holding the rules that stand for it on
 * the server now (one per simulator it was set up on). A switch removes those
 * rules from the old server (moved, not copied: a mock left behind goes on
 * answering that machine's traffic) and the entry, with the session capture,
 * waits for the agent's next reservation on the new server: a mock is scoped
 * to a simulator, the old one's UDID means nothing there, and a client key may
 * only create mocks scoped to a device it holds. Moving the entry, not each
 * rule, is what keeps a mock from multiplying with every switch (review).
 */

import { discoverServer } from "./config.js";
import { apiRequest } from "./http.js";
import { isClientKey } from "./remote-paths.js";

type Server = ReturnType<typeof discoverServer>;

/** The server's defaults for a response spec it is given (MockResponseSpec). */
const RESPONSE_DEFAULTS = { status_code: 200, headers: { "content-type": "application/json" }, body: "" };

interface Mock {
  pattern: string;
  response: Record<string, unknown>;
  /** Scoped to one simulator where it was set, or mocking every device. */
  scoped: boolean;
  /** The rules standing for it on the server now: simulator (null: every device) → rule id. */
  rules: Map<string | null, string>;
  /** Moved here, and not yet set up on everything it should be. */
  waiting: boolean;
}

interface Capture {
  processes: string[];
  only?: boolean;
  allow_passthrough?: boolean;
  /** Simulators it is asked for on, on the server now. */
  on: Set<string>;
  waiting: boolean;
}

let mocks: Mock[] = [];
let captures: Capture[] = [];

function mockOf(ruleId: string): Mock | undefined {
  return mocks.find((m) => [...m.rules.values()].includes(ruleId));
}

export function recordMock(ruleId: string, mock: {
  pattern: string; response: Record<string, unknown>; simulator_udid: string | null;
}): void {
  mocks.push({ pattern: mock.pattern, response: { ...mock.response },
               scoped: mock.simulator_udid !== null,
               rules: new Map([[mock.simulator_udid, ruleId]]), waiting: false });
}

/** update_mock, as the server applies it: a pattern given replaces the
 * pattern, and a response given replaces the whole response -- fields left out
 * take the server's defaults, not their old values (review). It changed that
 * one rule, so a mock standing on several simulators splits off the copy. */
export function updateMock(ruleId: string, change: {
  pattern?: string; response?: Record<string, unknown>; simulator_udid?: string | null;
}): void {
  let mock = mockOf(ruleId);
  if (!mock) return;
  const scope = [...mock.rules].find(([, id]) => id === ruleId)![0];
  if (mock.rules.size > 1) {
    mock.rules.delete(scope);
    mock = { ...mock, response: { ...mock.response }, rules: new Map([[scope, ruleId]]) };
    mocks.push(mock);
  }
  if (change.pattern !== undefined) mock.pattern = change.pattern;
  if (change.response !== undefined) mock.response = { ...RESPONSE_DEFAULTS, ...change.response };
  if (change.simulator_udid !== undefined) {
    mock.rules = new Map([[change.simulator_udid, ruleId]]);
    mock.scoped = change.simulator_udid !== null;
  }
}

/** A rule the agent removed, or the server reports gone (404). */
export function forgetMock(ruleId: string): void {
  const mock = mockOf(ruleId);
  if (!mock) return;
  for (const [scope, id] of mock.rules) if (id === ruleId) mock.rules.delete(scope);
  if (!mock.rules.size && !mock.waiting) mocks = mocks.filter((m) => m !== mock);
}

/** clear_mocks with no id: none of them, here or waiting to be set up. */
export function forgetAllMocks(): void {
  mocks = [];
}

export function recordCapture(simulator: string, capture: {
  processes: string[]; only?: boolean; allow_passthrough?: boolean;
}): void {
  const same = captures.find((c) => sameCapture(c, capture));
  if (same) {
    same.on.add(simulator);
    return;
  }
  captures.push({ ...capture, processes: [...capture.processes], on: new Set([simulator]),
                  waiting: false });
}

function sameCapture(a: { processes: string[]; only?: boolean; allow_passthrough?: boolean },
                     b: { processes: string[]; only?: boolean; allow_passthrough?: boolean }): boolean {
  return JSON.stringify([[...a.processes].sort(), !!a.only, !!a.allow_passthrough]) ===
    JSON.stringify([[...b.processes].sort(), !!b.only, !!b.allow_passthrough]);
}

/** remove_session_capture: one simulator's, some processes, or all. */
export function forgetCapture(simulator?: string, processes?: string[]): void {
  for (const c of captures) {
    if (processes) c.processes = c.processes.filter((p) => !processes.includes(p));
    if (simulator) c.on.delete(simulator); else if (!processes) c.on.clear();
  }
  captures = captures.filter((c) => c.processes.length && (c.on.size || c.waiting));
}

/** release_devices: what the server ends with a hold ends here too -- session
 * capture on those devices for every key, and a client key's mocks scoped to
 * them (server/api/sessions.py _end_client_work). Otherwise a mock the server
 * deleted came back at the next switch (review). Not called for the release a
 * switch makes, whose mocks move. */
export function released(udids: string[]): void {
  const client = isClientKey(discoverServer().apiKey);
  for (const udid of udids) {
    if (client) {
      for (const m of mocks) m.rules.delete(udid);
    }
    for (const c of captures) c.on.delete(udid);
  }
  mocks = mocks.filter((m) => m.rules.size || m.waiting);
  captures = captures.filter((c) => c.on.size || c.waiting);
}

export function pending(): { mocks: number; session_capture: number } {
  return { mocks: mocks.filter((m) => m.waiting).length,
           session_capture: captures.filter((c) => c.waiting).length };
}

/** At a switch: take this agent's mocks off `old` and keep them, with its
 * session capture, for the next reservation on the new server. Never throws. */
export async function moveOut(old: Server): Promise<{
  mocks: number; session_capture: number; still_on_old?: string[];
}> {
  const stillThere: string[] = [];
  for (const mock of mocks) {
    for (const ruleId of mock.rules.values()) {
      try {
        await apiRequest("DELETE", `/api/v1/proxy/mocks/${encodeURIComponent(ruleId)}`,
          undefined, undefined, 5000, old);
      } catch (e) {
        // Gone already -- a client's scoped mock ends with its hold -- is moved.
        if (!(e instanceof Error && e.message.startsWith("HTTP 404"))) stillThere.push(ruleId);
      }
    }
    mock.rules = new Map();
    mock.waiting = true;
  }
  for (const c of captures) {
    c.on = new Set();
    c.waiting = true;
  }
  return { ...pending(), ...(stillThere.length ? { still_on_old: stillThere } : {}) };
}

interface Reserved { udid?: string; device_type?: string }

/** After a reservation on the server switched to: set up what was moved on
 * the simulators just reserved. Each mock and capture is set up once per
 * simulator; what fails stays waiting for that simulator alone, so a retry
 * never doubles what already stands (review). Call it inside betweenSwitches,
 * so two reservations, or a reservation and a switch, never apply it at once.
 * Undefined when nothing was waiting. */
export async function applyOnReserve(reserved: Reserved[]): Promise<Record<string, unknown> | undefined> {
  const moved = mocks.filter((m) => m.waiting);
  const movedCaptures = captures.filter((c) => c.waiting);
  if (!moved.length && !movedCaptures.length) return undefined;
  const simulators = reserved.filter((d) => d.device_type === "simulator" && d.udid)
    .map((d) => String(d.udid));
  const everyDevice = !isClientKey(discoverServer().apiKey);
  const created: { pattern: string; rule_id: string; simulator_udid: string | null }[] = [];
  const failed: { pattern?: string; processes?: string[]; simulator_udid: string | null; error: string }[] = [];
  for (const mock of moved) {
    // Unscoped where it was, and this key may mock every device: unscoped.
    // Otherwise one rule per simulator just reserved.
    const scopes: (string | null)[] = !mock.scoped && everyDevice ? [null] : simulators;
    let complete = scopes.length > 0;
    for (const scope of scopes) {
      if (mock.rules.has(scope)) continue;          // set up on this one already
      try {
        const body: Record<string, unknown> = { pattern: mock.pattern, response: mock.response };
        if (scope !== null) body.simulator_udid = scope;
        const answer = (await apiRequest("POST", "/api/v1/proxy/mocks", undefined, body)) as
          { rule_id?: string } | null;
        if (!answer?.rule_id) throw new Error("no rule_id in the answer");
        mock.rules.set(scope, answer.rule_id);
        created.push({ pattern: mock.pattern, rule_id: answer.rule_id, simulator_udid: scope });
      } catch (e) {
        complete = false;
        failed.push({ pattern: mock.pattern, simulator_udid: scope,
                      error: e instanceof Error ? e.message : String(e) });
      }
    }
    if (complete) mock.waiting = false;
  }

  const captured: { simulator_udid: string; processes: string[] }[] = [];
  for (const capture of movedCaptures) {
    let complete = simulators.length > 0;
    for (const udid of simulators) {
      if (capture.on.has(udid)) continue;
      try {
        await apiRequest("POST", "/api/v1/proxy/session-capture", undefined, {
          simulator_udid: udid, processes: capture.processes,
          ...(capture.only ? { only: true } : {}),
          ...(capture.allow_passthrough ? { allow_passthrough: true } : {}),
        });
        capture.on.add(udid);
        captured.push({ simulator_udid: udid, processes: capture.processes });
      } catch (e) {
        complete = false;
        failed.push({ processes: capture.processes, simulator_udid: udid,
                      error: e instanceof Error ? e.message : String(e) });
      }
    }
    if (complete) capture.waiting = false;
  }

  const out: Record<string, unknown> = {};
  if (created.length) out.mocks_recreated = created;
  if (captured.length) out.session_capture_restored = captured;
  if (failed.length) out.failed = failed;
  const left = pending();
  if (left.mocks || left.session_capture) {
    out.still_waiting = left;
    out.note = simulators.length
      ? "Some could not be set up here (see failed): they are tried again at your next reservation."
      : "Mocks and session capture attach to simulators: they wait for a reservation that includes one.";
  }
  return out;
}

/** For tests: forget everything. */
export function resetCarried(): void {
  mocks = [];
  captures = [];
}
