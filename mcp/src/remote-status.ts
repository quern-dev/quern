/** The paired remotes and this Mac's own server, each asked now (#605).
 *
 * A port of server/remote/status.py, so list_remotes works where only the MCP
 * is installed -- a Linux or Windows machine paired with a Mac has no quern
 * CLI to run `quern remote list`. The two must agree: the same row shape, the
 * same words for why a server could not be asked, the same rule that a server
 * that could not be asked is never reported as having no devices.
 * tests/fixtures/remote-status.json holds the cases both suites run.
 *
 * Each server is asked in parallel with a short timeout, so one that is down
 * costs one timeout, not one per server, and the whole list stops waiting at
 * a deadline.
 */

import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";

import { API_KEY_FILE, LOCAL, readStateFile } from "./config.js";
import { quernHeaders, serverFetch } from "./http.js";
import { normalise, PinMismatch, REMOTES_FILE } from "./remote.js";

/** Per request, and for the whole list (status.py's TIMEOUT_SECONDS and
 * DEADLINE_SECONDS). */
export const TIMEOUT_MS = 4_000;
export const DEADLINE_MS = 10_000;

/** The session a status read names: a client key must name one, even to
 * read. Its own, so the read is never mistaken for the agent's work. */
const STATUS_SESSION = "quern-remote-status";

export interface Target {
  name: string;
  url: string;
  key: string;
  /** Empty for this Mac's own server: plain http on loopback, no pin. */
  fingerprint: string;
  label: string;
}

export type Row = Record<string, unknown>;

export class Unexpected extends Error {}

export class HttpStatus extends Error {
  constructor(readonly status: number) {
    super(`HTTP ${status}`);
  }
}

function blank(name: string | null, url: string | null, label: string | null): Row {
  return { name, url, label, reachable: false, version: null, version_matches: null,
           devices: null, free: null, error: null, disk: null, disk_error: null };
}

/** The remotes in remotes.json: {} when there is no file, and a thrown error
 * -- never an empty list -- when there is one that cannot be read. */
export function readRemotes(file = REMOTES_FILE): Record<string, unknown> {
  let text: string;
  try {
    text = readFileSync(file, "utf-8");
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw new Error(`${file} is not readable (${(e as Error).message}); fix or move it aside`);
  }
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch (e) {
    throw new Error(`${file} is not readable (${(e as Error).message}); fix or move it aside`);
  }
  const remotes = (data as { remotes?: unknown })?.remotes;
  if (!remotes || typeof remotes !== "object" || Array.isArray(remotes)) {
    throw new Error(`${file} has no 'remotes' object; fix or move it aside`);
  }
  return remotes as Record<string, unknown>;
}

/** Python's str() of a JSON value, which client.load applies to each field:
 * so a numeric key is accepted as its digits, and null reads "None". */
function pyStr(v: unknown): string {
  if (v === null || v === undefined) return "None";
  if (v === true) return "True";
  if (v === false) return "False";
  if (typeof v === "string") return v;
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}

/** Python's repr() of a string, which the messages quote names with. */
export function pyRepr(s: string): string {
  if (s.includes("'") && !s.includes('"')) return `"${s.replaceAll("\\", "\\\\")}"`;
  return `'${s.replaceAll("\\", "\\\\").replaceAll("'", "\\'")}'`;
}

/** Python's truthiness: empty strings, lists, objects, 0 and null are false. */
function truthy(v: unknown): boolean {
  if (Array.isArray(v)) return v.length > 0;
  if (v && typeof v === "object") return Object.keys(v).length > 0;
  return Boolean(v);
}

/** One entry as a target, or why it cannot be one, in client.load's words:
 * the url first, then the fingerprint, then the key, as its constructor
 * reads them. */
export function loadTarget(name: string, entry: unknown, file = REMOTES_FILE): Target | string {
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
    return `no remote called ${pyRepr(name)}: pair with one first ` +
      `(quern connect <url> --code <code> --name ${name})`;
  }
  const e = entry as Record<string, unknown>;
  const incomplete = (why: string) =>
    `the remote ${pyRepr(name)} in ${file} is incomplete (${why}); pair with it again`;
  if (!pyStr(e.url ?? "").toLowerCase().startsWith("https://")) {
    return `the remote ${pyRepr(name)} in ${file} is not an https:// address: its key would ` +
      "travel in clear; pair with it again";
  }
  if (!("fingerprint" in e)) return incomplete("'fingerprint'");
  let fingerprint: string;
  try {
    fingerprint = normalise(pyStr(e.fingerprint));
  } catch {
    return incomplete(`not a SHA-256 fingerprint: ${pyRepr(pyStr(e.fingerprint))}`);
  }
  if (!("key" in e)) return incomplete("'key'");
  return { name, url: pyStr(e.url).replace(/\/+$/, ""), key: pyStr(e.key), fingerprint,
           // An explicit null is no label, not the text "None" (#607).
           label: e.label === null || e.label === undefined ? "" : pyStr(e.label) };
}

async function get(target: Target, path: string, timeoutMs: number): Promise<unknown> {
  const resp = await serverFetch(
    // No fingerprint: this Mac's server, over a direct connection that never
    // takes a proxy from the environment (serverFetch).
    target.fingerprint ? { fingerprint: target.fingerprint } : {},
    target.url + path,
    { headers: { ...quernHeaders(target.key), "X-Quern-Session": STATUS_SESSION },
      signal: AbortSignal.timeout(timeoutMs) },
  );
  if (!resp.ok) throw new HttpStatus(resp.status);
  const body = await resp.text();
  try {
    return JSON.parse(body);
  } catch {
    throw new Unexpected("not JSON");
  }
}

/** Why a server could not be asked, in status.py's words. A network error's
 * own text is the runtime's: Python quotes its errno, this the code. */
export function reason(e: unknown): string {
  const cause = (e as { cause?: unknown })?.cause;
  if (e instanceof PinMismatch) return `certificate changed: ${e.message}`;
  if (cause instanceof PinMismatch) return `certificate changed: ${cause.message}`;
  if (e instanceof Unexpected) return `answered with something that is not quern's (${e.message})`;
  if (e instanceof HttpStatus) {
    if (e.status === 401) {
      return "refused its key (HTTP 401): it was revoked, or the server was reset";
    }
    if (e.status === 403) return "refused this request (HTTP 403)";
    return `HTTP ${e.status}`;
  }
  const name = (e as { name?: string })?.name;
  if (name === "TimeoutError" || name === "AbortError") return "not reachable (timed out)";
  const code = (cause as { code?: string })?.code ?? (e as { code?: string })?.code;
  if (code) return `not reachable (${code})`;
  const err = (cause ?? e) as Error;
  return `could not be asked (${err?.name ?? "Error"}: ${err?.message ?? String(err)})`;
}

/** The version a quern /health answer carries; Unexpected otherwise. */
export function health(answer: unknown): string {
  const a = answer as { status?: unknown; version?: unknown } | null;
  if (!a || typeof a !== "object" || Array.isArray(a) || a.status !== "ok" ||
      typeof a.version !== "string") {
    throw new Unexpected("no quern health answer");
  }
  return a.version;
}

/** Devices from a device-list answer. Anything that is not a list of devices
 * is Unexpected, never read as "none". Truthiness is Python's, so the two
 * lists agree on what a malformed server sends (#607). */
export function devices(answer: unknown, label = ""): Row[] {
  const rows = answer && typeof answer === "object" && !Array.isArray(answer)
    ? (answer as { devices?: unknown }).devices : undefined;
  if (!Array.isArray(rows) || !rows.every((d) => d && typeof d === "object" && !Array.isArray(d))) {
    throw new Unexpected("no device list");
  }
  return (rows as Record<string, unknown>[]).map((d) => {
    const held = d.reservation;
    if (held !== undefined && held !== null && (typeof held !== "object" || Array.isArray(held))) {
      throw new Unexpected("a device's reservation is not an object");
    }
    const h = (held ?? null) as Record<string, unknown> | null;
    return {
      udid: d.udid ?? null, name: d.name ?? null,
      os_version: d.os_version ?? null, device_type: d.device_type ?? null,
      device_family: d.device_family ?? null,
      model: d.model ?? null,
      ...(truthy(d.ca_install) ? { ca_install: d.ca_install } : {}),
      state: d.state ?? null, is_available: d.is_available ?? null,
      held_by: truthy(h) ? (h!.held_by ?? null) : null,
      free: h === null,
      // Held by this key through any of its sessions: a server that says
      // (this_key) is believed, an older one read by its sessions' label.
      yours: truthy(h) && (truthy(h!.this_key) || truthy(h!.yours) || (
        Boolean(label) && pyStr(h!.held_by ?? "").startsWith(`${label}:`))),
    };
  });
}

/** A server's disk use (#581), or why it could not be read: a server from
 * before it answers 404, which is "not reported", not "nothing used". As
 * status.py's _disk, which a fixture case pins. */
async function disk(target: Target, timeoutMs: number): Promise<[Row | null, string | null]> {
  let report: unknown;
  try {
    report = await get(target, "/api/v1/system/disk", timeoutMs);
  } catch (e) {
    return [null, `disk use could not be read: ${reason(e)}`];
  }
  return diskRow(report);
}

/** A /system/disk answer as the row's `disk` and `disk_error`. */
export function diskRow(report: unknown): [Row | null, string | null] {
  if (!report || typeof report !== "object" || Array.isArray(report)) {
    return [null, "disk use could not be read: not a report"];
  }
  const r = report as Record<string, unknown>;
  if (truthy(r.measuring)) return [{ measuring: true }, null];
  const stores = r.stores;
  if (truthy(r.error) && !(stores && typeof stores === "object" && !Array.isArray(stores))) {
    return [null, `disk use could not be measured there: ${pyStr(r.error)}`];
  }
  const d = (r.disk && typeof r.disk === "object" && !Array.isArray(r.disk)
    ? r.disk : {}) as Record<string, unknown>;
  const warnings = truthy(r.warnings) ? r.warnings : [];
  if (!Array.isArray(warnings)) {
    return [null, "disk use could not be read: its warnings are not a list"];
  }
  return [{
    used_bytes: r.used_bytes ?? null,
    used_complete: r.used_complete ?? null,
    free_bytes: d.free_bytes ?? null,
    total_bytes: d.total_bytes ?? null,
    warnings: [
      ...warnings,
      ...(truthy(r.last_error) ? [`the latest measurement failed: ${pyStr(r.last_error)}`] : []),
      ...(truthy(d.error) ? [`free space could not be measured: ${pyStr(d.error)}`] : []),
    ],
    measured_at: r.measured_at ?? null,
  }, null];
}

/** One server's state now. Never throws: one server's odd answer must not
 * cost the list every other server. version_matches is set by the caller,
 * against this machine's version. `answered` is handed the row before its
 * disk use is asked, so a deadline that falls during that third read keeps
 * the version and devices already read. */
export async function probe(target: Target, timeoutMs = TIMEOUT_MS,
                            answered?: (row: Row) => void): Promise<Row> {
  const out = blank(target.name, target.url, target.label);
  let version: string;
  try {
    version = health(await get(target, "/health", timeoutMs));
  } catch (e) {
    out.error = reason(e);
    return out;
  }
  out.reachable = true;
  out.version = version;
  try {
    const list = devices(await get(target, "/api/v1/device/list", timeoutMs), target.label);
    out.devices = list;
    out.free = list.filter((d) => d.free).length;
  } catch (e) {
    out.error = `devices could not be listed: ${reason(e)}`;
    return out;
  }
  answered?.({ ...out, disk_error: "disk use did not answer in time" });
  try {
    [out.disk, out.disk_error] = await disk(target, timeoutMs);
  } catch (e) {
    out.disk = null;
    out.disk_error = `disk use could not be read: ${reason(e)}`;
  }
  return out;
}

/** This Mac's server listens on `port`: the pid lsof reports, or null when it
 * cannot say (no lsof, nothing listening), as ports._get_pid_on_port. */
export function pidOnPort(port: number): Promise<number | null> {
  return new Promise((resolve) => {
    execFile("lsof", ["-ti", `TCP:${port}`, "-sTCP:LISTEN"], { timeout: 5000 }, (err, out) => {
      const first = Number.parseInt(String(out ?? "").trim().split("\n")[0] ?? "", 10);
      resolve(err || Number.isNaN(first) ? null : first);
    });
  });
}

const NOT_RUNNING = "not running here (quern start)";

/** The key a quern client on this machine sends (config.client_api_key):
 * QUERN_CLIENT_KEY, else the owner's key file. */
function clientApiKey(): string {
  const explicit = process.env.QUERN_CLIENT_KEY?.trim();
  if (explicit) return explicit;
  try {
    return readFileSync(API_KEY_FILE, "utf-8").trim();
  } catch {
    return "";
  }
}

/** This Mac's own server, as `quern url` finds it (__main__._server_base_url):
 * state.json's port on loopback, and only while the pid listening there is
 * the one it recorded -- a process that took a dead server's port is never
 * handed the key. Not QUERN_SERVER_URL, which can point anywhere. Health is
 * asked by the caller; any failure there also reads "not running here". */
export async function local(lookupPid = pidOnPort): Promise<Target | Row> {
  const notRunning = { ...blank(LOCAL, null, "this Mac"), local: true, error: NOT_RUNNING };
  const state = readStateFile();
  const port: unknown = state?.server_port;
  if (!state || typeof port !== "number" || !Number.isInteger(port) || port < 1 || port > 65535) {
    return notRunning;
  }
  if (Number.isInteger(state.pid)) {
    const listener = await lookupPid(port);
    if (listener !== null && listener !== state.pid) return notRunning;
  }
  return { name: LOCAL, url: `http://127.0.0.1:${port}`, key: clientApiKey(),
           fingerprint: "", label: "this Mac", isLocal: true } as Target;
}

const isTarget = (x: Target | Row): x is Target => typeof (x as Target).key === "string" &&
  typeof (x as Target).fingerprint === "string";

/** Every paired remote in name order, this Mac's server first: what
 * `quern remote list --json --with-local` prints. The version compared against
 * is this Mac's server's when it answers, else `ownVersion` (this MCP's). */
export async function remoteList(
  { ownVersion = null as string | null, file = REMOTES_FILE, timeoutMs = TIMEOUT_MS,
    deadlineMs = DEADLINE_MS, localServer = local }: {
    ownVersion?: string | null; file?: string; timeoutMs?: number; deadlineMs?: number;
    /** This Mac's server, injectable so a test never asks the real one. */
    localServer?: () => Target | Row | Promise<Target | Row>;
  } = {},
): Promise<{ local_version: string; remotes: Row[] }> {
  const ends = Date.now() + deadlineMs;
  let names: string[];
  let entries: Record<string, unknown>;
  try {
    entries = readRemotes(file);
    names = Object.keys(entries).sort();
  } catch (e) {
    return { local_version: ownVersion ?? "unknown",
             remotes: [{ name: null, error: (e as Error).message }] };
  }
  const loaded: (Target | Row)[] = names.map((name) => {
    const t = loadTarget(name, entries[name], file);
    return typeof t === "string" ? { name, url: null, reachable: false, error: t } : t;
  });
  try {
    loaded.unshift(await localServer());
  } catch (e) {
    loaded.unshift({ ...blank(LOCAL, null, "this Mac"), local: true,
                     error: `could not be asked (${(e as Error).message})` });
  }
  const rows = await Promise.all(loaded.map(async (t) => {
    if (!isTarget(t)) return t;
    const late = { ...blank(t.name, t.url, t.label),
                   error: `not answering (no complete answer within ${Math.round(deadlineMs / 1000)}s)` };
    // The latest the probe has answered, so a deadline during the disk read
    // keeps the version and devices (status.py's _probe_all).
    let partial: Row | undefined;
    let timer: NodeJS.Timeout | undefined;
    const deadline = new Promise<Row>((resolve) => {
      timer = setTimeout(() => resolve(partial ?? late), Math.max(0, ends - Date.now()));
    });
    try {
      const row = await Promise.race([probe(t, timeoutMs, (r) => { partial = r; }), deadline]);
      // status.py asks this Mac's server for its health before listing it,
      // and reads any failure as not running -- never a remote's row.
      if ((t as { isLocal?: boolean }).isLocal && !row.reachable) {
        return { ...row, error: NOT_RUNNING };
      }
      return row;
    } finally {
      clearTimeout(timer);
    }
  }));
  const here = rows[0];
  const baseline = here?.reachable && typeof here.version === "string" ? here.version : ownVersion;
  for (const row of rows) {
    // By name and label, as status.py marks it: a remote paired as "local"
    // (which `quern connect` refuses) is not this Mac unless it is labelled so.
    row.local = row.name === LOCAL && row.label === "this Mac";
    if (row.reachable && baseline) row.version_matches = row.version === baseline;
  }
  return { local_version: baseline ?? "unknown", remotes: rows };
}
