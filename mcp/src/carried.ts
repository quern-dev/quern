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
import { ServerTimeout, ServerUnreachable, apiRequest } from "./http.js";
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
  /** Where setting it up failed on this server: retried when that simulator is reserved. */
  failed: Set<string | null>;
  /** Where the agent removed it, or the server ended it, on this server: never put back. */
  removed: Set<string | null>;
  /** Still to be set up: failed somewhere, or moved here and set up nowhere yet. */
  waiting: boolean;
}

/** Waiting, from what stands, failed and was removed (review: a flag set once
 * a reservation went through dropped failures that were never retried). */
function mockWaiting(m: Mock): boolean {
  return m.failed.size > 0 || (m.rules.size === 0 && m.removed.size === 0);
}

interface Capture {
  processes: string[];
  only?: boolean;
  allow_passthrough?: boolean;
  /** Simulators it is asked for on, on the server now. */
  on: Set<string>;
  /** Waiting: the simulators it was asked for on where it was set, so removing
   * it by one of those still cancels it (CodeRabbit). */
  from: Set<string>;
  /** Simulators on the server now that the agent removed it from: a retry
   * for the others must not put it back on them (CodeRabbit). */
  removed: Set<string>;
  /** Simulators on the server now where asking for it failed: retried there. */
  failed: Set<string>;
  waiting: boolean;
}

/** Waiting: failed somewhere here, or moved and asked for nowhere yet -- and
 * not cancelled by every simulator it came from. */
function captureWaiting(c: Capture): boolean {
  return c.failed.size > 0 || (c.on.size === 0 && c.removed.size === 0 && c.from.size > 0);
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
               rules: new Map([[mock.simulator_udid, ruleId]]),
               failed: new Set(), removed: new Set(), waiting: false });
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
    mock = { ...mock, response: { ...mock.response }, rules: new Map([[scope, ruleId]]),
             failed: new Set(), removed: new Set(), waiting: false };
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
  for (const [scope, id] of mock.rules) {
    if (id !== ruleId) continue;
    mock.rules.delete(scope);
    mock.removed.add(scope);
  }
  if (mock.waiting) mock.waiting = mockWaiting(mock);
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
                  from: new Set(), removed: new Set(), failed: new Set(), waiting: false });
}

function sameCapture(a: { processes: string[]; only?: boolean; allow_passthrough?: boolean },
                     b: { processes: string[]; only?: boolean; allow_passthrough?: boolean }): boolean {
  return JSON.stringify([[...a.processes].sort(), !!a.only, !!a.allow_passthrough]) ===
    JSON.stringify([[...b.processes].sort(), !!b.only, !!b.allow_passthrough]);
}

/** remove_session_capture: one simulator's, some processes, or all. */
export function forgetCapture(simulator?: string, processes?: string[]): void {
  // An empty list is the server's "all of them", as an omitted one is (CodeRabbit).
  const named = processes?.length ? processes : undefined;
  if (!simulator && !named) {
    captures = [];          // all of it: here, and what waits to be set up (CodeRabbit)
    return;
  }
  if (!simulator) {
    for (const c of captures) c.processes = c.processes.filter((p) => !named!.includes(p));
  } else {
    // That simulator's capture, or some of its processes there: what stays
    // on it is split off, as the server keeps it. A capture waiting after a
    // switch is matched by the simulator it was asked for on (CodeRabbit).
    const split: Capture[] = [];
    for (const c of captures) {
      // Here: standing, or failed and waiting for a retry. From the old
      // server: a waiting capture matched by the simulator it came from.
      const standing = c.on.has(simulator);
      const failedHere = c.failed.has(simulator);
      const fromOld = !standing && !failedHere && c.waiting && c.from.has(simulator);
      if (!standing && !failedHere && !fromOld) continue;
      if (fromOld) {
        c.from.delete(simulator);
      } else {
        c.on.delete(simulator);
        c.failed.delete(simulator);
        c.removed.add(simulator);
      }
      const kept = named ? c.processes.filter((p) => !named.includes(p)) : [];
      if (kept.length) {
        split.push({ ...c, processes: kept,
                     on: standing ? new Set([simulator]) : new Set(),
                     failed: failedHere ? new Set([simulator]) : new Set(),
                     from: fromOld ? new Set([simulator]) : new Set(),
                     removed: new Set() });
      }
      if (c.waiting) c.waiting = captureWaiting(c);
    }
    for (const c of split) c.waiting = c.waiting && captureWaiting(c);
    captures.push(...split);
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
    for (const m of mocks) {
      // Released, a device is not retried; a client's rule on it is gone.
      if (m.failed.delete(udid)) m.removed.add(udid);
      if (client && m.rules.delete(udid)) m.removed.add(udid);
    }
    for (const c of captures) {
      if (c.failed.delete(udid)) c.removed.add(udid);
      if (c.on.delete(udid)) c.removed.add(udid);
    }
  }
  for (const m of mocks) if (m.waiting) m.waiting = mockWaiting(m);
  for (const c of captures) if (c.waiting) c.waiting = captureWaiting(c);
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
  // A server that timed out or cannot be reached is not asked again for each
  // rule: that held every switch and reservation behind it (CodeRabbit).
  let gone = false;
  for (const mock of mocks) {
    for (const ruleId of mock.rules.values()) {
      if (gone) {
        stillThere.push(ruleId);
        continue;
      }
      try {
        await apiRequest("DELETE", `/api/v1/proxy/mocks/${encodeURIComponent(ruleId)}`,
          undefined, undefined, 5000, old);
      } catch (e) {
        // Gone already -- a client's scoped mock ends with its hold -- is moved.
        if (!(e instanceof Error && e.message.startsWith("HTTP 404"))) stillThere.push(ruleId);
        if (e instanceof ServerTimeout || e instanceof ServerUnreachable) gone = true;
      }
    }
    mock.rules = new Map();
    mock.failed = new Set();      // a new server: its simulators are all new
    mock.removed = new Set();
    mock.waiting = true;
  }
  for (const c of captures) {
    for (const udid of c.on) c.from.add(udid);
    c.on = new Set();
    c.removed = new Set();        // a new server: its simulators are all new
    c.failed = new Set();
    c.waiting = true;
  }
  return { ...pending(), ...(stillThere.length ? { still_on_old: stillThere } : {}) };
}

interface Reserved { udid?: string; device_type?: string }

interface ServerMock { rule_id: string; pattern: string; response: unknown; simulator_udid?: string | null }

/** A response as text, keys sorted, so two that say the same compare equal. */
function canonical(value: unknown): string {
  return JSON.stringify(value, (_k, v) => v && typeof v === "object" && !Array.isArray(v)
    ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => a.localeCompare(b))) : v);
}

/** Each request that sets something up again: it holds the switch queue. */
const SETUP_TIMEOUT_MS = 15_000;

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
  // A failed attempt may have made its rule anyway -- a timeout says nothing
  // either way, and the server takes no idempotency key -- so a retry first
  // looks for it, once per reservation, and adopts it (CodeRabbit).
  let onServer: ServerMock[] | null | undefined;
  const adoptable = async (mock: Mock, scope: string | null): Promise<string | undefined> => {
    if (onServer === undefined) {
      try {
        onServer = ((await apiRequest("GET", "/api/v1/proxy/mocks", undefined, undefined,
          SETUP_TIMEOUT_MS)) as { rules?: ServerMock[] } | null)?.rules ?? [];
      } catch {
        onServer = null;          // could not look: retried as before
      }
    }
    if (!onServer) return undefined;
    const taken = new Set(mocks.flatMap((m) => [...m.rules.values()]));
    const want = canonical({ ...RESPONSE_DEFAULTS, ...mock.response });
    return onServer.find((r) => !taken.has(r.rule_id) && r.pattern === mock.pattern &&
      (r.simulator_udid ?? null) === scope && canonical(r.response) === want)?.rule_id;
  };
  for (const mock of moved) {
    // Unscoped where it was, and this key may mock every device: unscoped.
    // Otherwise one rule per simulator just reserved.
    const scopes: (string | null)[] = !mock.scoped && everyDevice ? [null] : simulators;
    for (const scope of scopes) {
      // Set up there already, or removed there by the agent: left alone.
      if (mock.rules.has(scope) || mock.removed.has(scope)) continue;
      const found = mock.failed.has(scope) ? await adoptable(mock, scope) : undefined;
      if (found) {
        mock.rules.set(scope, found);
        mock.failed.delete(scope);
        created.push({ pattern: mock.pattern, rule_id: found, simulator_udid: scope });
        continue;
      }
      try {
        const body: Record<string, unknown> = { pattern: mock.pattern, response: mock.response };
        if (scope !== null) body.simulator_udid = scope;
        // Bounded: this runs in the switch queue, and what times out waits.
        const answer = (await apiRequest("POST", "/api/v1/proxy/mocks", undefined, body,
          SETUP_TIMEOUT_MS)) as
          { rule_id?: string } | null;
        if (!answer?.rule_id) throw new Error("no rule_id in the answer");
        mock.rules.set(scope, answer.rule_id);
        mock.failed.delete(scope);
        created.push({ pattern: mock.pattern, rule_id: answer.rule_id, simulator_udid: scope });
      } catch (e) {
        mock.failed.add(scope);
        failed.push({ pattern: mock.pattern, simulator_udid: scope,
                      error: e instanceof Error ? e.message : String(e) });
      }
    }
    mock.waiting = mockWaiting(mock);
  }

  const captured: { simulator_udid: string; processes: string[] }[] = [];
  for (const capture of movedCaptures) {
    for (const udid of simulators) {
      if (capture.on.has(udid) || capture.removed.has(udid)) continue;
      try {
        await apiRequest("POST", "/api/v1/proxy/session-capture", undefined, {
          simulator_udid: udid, processes: capture.processes,
          ...(capture.only ? { only: true } : {}),
          ...(capture.allow_passthrough ? { allow_passthrough: true } : {}),
        }, SETUP_TIMEOUT_MS);
        capture.on.add(udid);
        capture.failed.delete(udid);
        captured.push({ simulator_udid: udid, processes: capture.processes });
      } catch (e) {
        capture.failed.add(udid);
        failed.push({ processes: capture.processes, simulator_udid: udid,
                      error: e instanceof Error ? e.message : String(e) });
      }
    }
    capture.waiting = captureWaiting(capture);
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
