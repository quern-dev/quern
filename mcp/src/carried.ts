/** What this agent set up on its server that follows it to another (use_remote).
 *
 * The server cannot say which mocks are this agent's: it records an owner per
 * key, and every MCP on the server's own Mac uses the same owner key. So this
 * MCP keeps its own record of the mocks and session capture it set, as it set
 * them -- a client key's scoped mocks are deleted by the server when its
 * device is released, which a switch requires, so they cannot be read back
 * then.
 *
 * At a switch the mocks are removed from the old server (moved, not copied: a
 * mock left behind goes on answering that machine's traffic) and, with the
 * session capture, wait for the agent's next reservation on the new server:
 * a mock is scoped to a simulator, the old one's UDID means nothing there,
 * and a client key may only create mocks scoped to a device it holds.
 */

import { discoverServer } from "./config.js";
import { apiRequest } from "./http.js";
import { isClientKey } from "./remote-paths.js";

type Server = ReturnType<typeof discoverServer>;

export interface CarriedMock {
  pattern: string;
  response: Record<string, unknown>;
  /** The simulator it was scoped to where it was set; null for every device. */
  simulator_udid: string | null;
}

export interface CarriedCapture {
  processes: string[];
  only?: boolean;
  allow_passthrough?: boolean;
}

/** Mocks this agent set on the server it uses now, by rule id. */
const mocks = new Map<string, CarriedMock>();
/** Session capture it asked for there, by simulator. */
const captures = new Map<string, CarriedCapture[]>();
/** Moved from another server, waiting for a reservation here. */
let pendingMocks: CarriedMock[] = [];
let pendingCaptures: CarriedCapture[] = [];

export function recordMock(ruleId: string, mock: CarriedMock): void {
  mocks.set(ruleId, mock);
}

/** update_mock's change, applied as the server applies it: only what was given. */
export function updateMock(ruleId: string, change: {
  pattern?: string; response?: Record<string, unknown>; simulator_udid?: string | null;
}): void {
  const mock = mocks.get(ruleId);
  if (!mock) return;
  if (change.pattern !== undefined) mock.pattern = change.pattern;
  if (change.response !== undefined) mock.response = { ...mock.response, ...change.response };
  if (change.simulator_udid !== undefined) mock.simulator_udid = change.simulator_udid;
}

export function forgetMock(ruleId: string): void {
  mocks.delete(ruleId);
}

/** clear_mocks with no id: none of them, here or waiting to be recreated. */
export function forgetAllMocks(): void {
  mocks.clear();
  pendingMocks = [];
}

export function recordCapture(simulator: string, capture: CarriedCapture): void {
  const list = captures.get(simulator) ?? [];
  list.push(capture);
  captures.set(simulator, list);
}

/** remove_session_capture: one simulator's, some processes, or all. */
export function forgetCapture(simulator?: string, processes?: string[]): void {
  for (const [udid, list] of [...captures]) {
    if (simulator && udid !== simulator) continue;
    if (!processes) {
      captures.delete(udid);
      continue;
    }
    const kept = list
      .map((c) => ({ ...c, processes: c.processes.filter((p) => !processes.includes(p)) }))
      .filter((c) => c.processes.length);
    if (kept.length) captures.set(udid, kept); else captures.delete(udid);
  }
}

export function pending(): { mocks: number; session_capture: number } {
  return { mocks: pendingMocks.length, session_capture: pendingCaptures.length };
}

/** At a switch: take this agent's mocks off `old` and keep them, with its
 * session capture, for the next reservation on the new server. Never throws. */
export async function moveOut(old: Server): Promise<{
  mocks: number; session_capture: number; still_on_old?: string[];
}> {
  const stillThere: string[] = [];
  for (const [ruleId, mock] of mocks) {
    try {
      await apiRequest("DELETE", `/api/v1/proxy/mocks/${encodeURIComponent(ruleId)}`,
        undefined, undefined, 5000, old);
    } catch (e) {
      // Gone already -- a client's scoped mock ends with its hold -- is moved.
      if (!(e instanceof Error && e.message.startsWith("HTTP 404"))) stillThere.push(ruleId);
    }
    pendingMocks.push({ ...mock, response: { ...mock.response } });
  }
  mocks.clear();
  for (const list of captures.values()) pendingCaptures.push(...list);
  captures.clear();
  // The same processes asked for on several simulators are asked for once.
  const seen = new Set<string>();
  pendingCaptures = pendingCaptures.filter((c) => {
    const key = JSON.stringify([[...c.processes].sort(), !!c.only, !!c.allow_passthrough]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return { ...pending(), ...(stillThere.length ? { still_on_old: stillThere } : {}) };
}

interface Reserved { udid?: string; device_type?: string; name?: string }

/** After a reservation on the server switched to: recreate what was moved,
 * scoped to the simulators just reserved. What cannot be recreated yet stays
 * waiting, and the report says why. Undefined when nothing was waiting. */
export async function applyOnReserve(reserved: Reserved[]): Promise<Record<string, unknown> | undefined> {
  if (!pendingMocks.length && !pendingCaptures.length) return undefined;
  const simulators = reserved.filter((d) => d.device_type === "simulator" && d.udid)
    .map((d) => String(d.udid));
  const everyDevice = !isClientKey(discoverServer().apiKey);
  const created: { pattern: string; rule_id: string; simulator_udid: string | null }[] = [];
  const failed: { pattern: string; error: string }[] = [];
  const waiting: CarriedMock[] = [];
  for (const mock of pendingMocks) {
    // Unscoped where it was, and this key may mock every device: unscoped.
    // Otherwise one rule per simulator just reserved.
    const scopes: (string | null)[] = mock.simulator_udid === null && everyDevice
      ? [null] : simulators;
    if (!scopes.length) {
      waiting.push(mock);
      continue;
    }
    let ok = true;
    for (const scope of scopes) {
      try {
        const body: Record<string, unknown> = { pattern: mock.pattern, response: mock.response };
        if (scope !== null) body.simulator_udid = scope;
        const answer = (await apiRequest("POST", "/api/v1/proxy/mocks", undefined, body)) as
          { rule_id?: string } | null;
        if (!answer?.rule_id) throw new Error("no rule_id in the answer");
        recordMock(answer.rule_id, { ...mock, simulator_udid: scope });
        created.push({ pattern: mock.pattern, rule_id: answer.rule_id, simulator_udid: scope });
      } catch (e) {
        ok = false;
        failed.push({ pattern: mock.pattern, error: e instanceof Error ? e.message : String(e) });
        break;
      }
    }
    if (!ok) waiting.push(mock);
  }
  pendingMocks = waiting;

  const captured: { simulator_udid: string; processes: string[] }[] = [];
  const captureFailed: { processes: string[]; error: string }[] = [];
  const capturesWaiting: CarriedCapture[] = [];
  for (const capture of pendingCaptures) {
    if (!simulators.length) {
      capturesWaiting.push(capture);
      continue;
    }
    let ok = true;
    for (const udid of simulators) {
      try {
        await apiRequest("POST", "/api/v1/proxy/session-capture", undefined, {
          simulator_udid: udid, processes: capture.processes,
          ...(capture.only ? { only: true } : {}),
          ...(capture.allow_passthrough ? { allow_passthrough: true } : {}),
        });
        recordCapture(udid, capture);
        captured.push({ simulator_udid: udid, processes: capture.processes });
      } catch (e) {
        ok = false;
        captureFailed.push({ processes: capture.processes,
                             error: e instanceof Error ? e.message : String(e) });
        break;
      }
    }
    if (!ok) capturesWaiting.push(capture);
  }
  pendingCaptures = capturesWaiting;

  const out: Record<string, unknown> = {};
  if (created.length) out.mocks_recreated = created;
  if (captured.length) out.session_capture_restored = captured;
  if (failed.length || captureFailed.length) {
    out.failed = [...failed, ...captureFailed];
  }
  if (pendingMocks.length || pendingCaptures.length) {
    out.still_waiting = pending();
    out.note = simulators.length
      ? "Some could not be set up here (see failed): they wait for your next reservation."
      : "Mocks and session capture attach to simulators: they wait for a reservation that includes one.";
  }
  return out;
}

/** For tests: forget everything. */
export function resetCarried(): void {
  mocks.clear();
  captures.clear();
  pendingMocks = [];
  pendingCaptures = [];
}
