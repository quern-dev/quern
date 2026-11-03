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

import { readFileSync } from "node:fs";

import { discoverServer, LOCAL } from "./config.js";
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

class Unexpected extends Error {}

class HttpStatus extends Error {
  constructor(readonly status: number) {
    super(`HTTP ${status}`);
  }
}

function blank(name: string | null, url: string | null, label: string | null): Row {
  return { name, url, label, reachable: false, version: null, version_matches: null,
           devices: null, free: null, error: null };
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

/** One entry as a target, or why it cannot be one (client.load's checks). */
export function loadTarget(name: string, entry: unknown, file = REMOTES_FILE): Target | string {
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
    return `no remote called '${name}': pair with one first ` +
      `(quern connect <url> --code <code> --name ${name})`;
  }
  const e = entry as Record<string, unknown>;
  if (!String(e.url ?? "").toLowerCase().startsWith("https://")) {
    return `the remote '${name}' in ${file} is not an https:// address: its key would ` +
      "travel in clear; pair with it again";
  }
  if (typeof e.key !== "string" || typeof e.fingerprint !== "string") {
    return `the remote '${name}' in ${file} is incomplete (no ` +
      `${typeof e.key !== "string" ? "key" : "fingerprint"}); pair with it again`;
  }
  let fingerprint: string;
  try {
    fingerprint = normalise(e.fingerprint);
  } catch (err) {
    return `the remote '${name}' in ${file} is incomplete (${(err as Error).message}); ` +
      "pair with it again";
  }
  return { name, url: String(e.url).replace(/\/+$/, ""), key: e.key, fingerprint,
           label: typeof e.label === "string" ? e.label : String(e.label ?? "") };
}

async function get(target: Target, path: string, timeoutMs: number): Promise<unknown> {
  const resp = await serverFetch(
    // No fingerprint: this Mac's server, over Node's own fetch, which reads no
    // proxy from the environment -- one would be handed the owner's key.
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

/** Why a server could not be asked, in status.py's words. */
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
  const message = (cause as Error)?.message ?? (e as Error)?.message ?? String(e);
  return `could not be asked (${message})`;
}

function health(answer: unknown): string {
  const a = answer as { status?: unknown; version?: unknown } | null;
  if (!a || typeof a !== "object" || a.status !== "ok" || typeof a.version !== "string") {
    throw new Unexpected("no quern health answer");
  }
  return a.version;
}

/** Devices from a device-list answer. Anything that is not a list of devices
 * is Unexpected, never read as "none". */
export function devices(answer: unknown, label = ""): Row[] {
  const rows = (answer as { devices?: unknown } | null)?.devices;
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
      ...(d.ca_install ? { ca_install: d.ca_install } : {}),
      state: d.state ?? null, is_available: d.is_available ?? null,
      held_by: h ? (h.held_by ?? null) : null,
      free: h === null,
      // Held by this key through any of its sessions: a server that says
      // (this_key) is believed, an older one read by its sessions' label.
      yours: Boolean(h && (h.this_key || h.yours || (
        label && String(h.held_by ?? "").startsWith(`${label}:`)))),
    };
  });
}

/** One server's state now. Never throws: one server's odd answer must not
 * cost the list every other server. */
export async function probe(target: Target, localVersion: string | null,
                            timeoutMs = TIMEOUT_MS): Promise<Row> {
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
  if (localVersion) out.version_matches = version === localVersion;
  try {
    const list = devices(await get(target, "/api/v1/device/list", timeoutMs), target.label);
    out.devices = list;
    out.free = list.filter((d) => d.free).length;
  } catch (e) {
    out.error = `devices could not be listed: ${reason(e)}`;
  }
  return out;
}

/** This Mac's own server, or the row saying why it cannot be asked. */
export function local(): Target | Row {
  const server = discoverServer("");
  if (server.source === "default") {
    return { ...blank(LOCAL, null, "this Mac"), local: true,
             error: "not running here (quern start)" };
  }
  return { name: LOCAL, url: server.url.replace(/\/+$/, ""), key: server.apiKey,
           fingerprint: "", label: "this Mac" };
}

const isTarget = (x: Target | Row): x is Target => typeof (x as Target).key === "string" &&
  typeof (x as Target).fingerprint === "string";

/** Every paired remote in name order, this Mac's server first: what
 * `quern remote list --json --with-local` prints. */
export async function remoteList(
  { localVersion = null as string | null, file = REMOTES_FILE, timeoutMs = TIMEOUT_MS,
    deadlineMs = DEADLINE_MS, localServer = local }: {
    localVersion?: string | null; file?: string; timeoutMs?: number; deadlineMs?: number;
    /** This Mac's server, injectable so a test never asks the real one. */
    localServer?: () => Target | Row;
  } = {},
): Promise<{ local_version: string | null; remotes: Row[] }> {
  const ends = Date.now() + deadlineMs;
  let names: string[];
  let entries: Record<string, unknown>;
  try {
    entries = readRemotes(file);
    names = Object.keys(entries).sort();
  } catch (e) {
    return { local_version: localVersion, remotes: [{ name: null, error: (e as Error).message }] };
  }
  const loaded: (Target | Row)[] = names.map((name) => {
    const t = loadTarget(name, entries[name], file);
    return typeof t === "string" ? { name, url: null, reachable: false, error: t } : t;
  });
  try {
    loaded.unshift(localServer());
  } catch (e) {
    loaded.unshift({ ...blank(LOCAL, null, "this Mac"), local: true,
                     error: `could not be asked (${(e as Error).message})` });
  }
  const rows = await Promise.all(loaded.map(async (t) => {
    if (!isTarget(t)) return t;
    const late = { ...blank(t.name, t.url, t.label),
                   error: `not answering (no complete answer within ${Math.round(deadlineMs / 1000)}s)` };
    let timer: NodeJS.Timeout | undefined;
    const deadline = new Promise<Row>((resolve) => {
      timer = setTimeout(() => resolve(late), Math.max(0, ends - Date.now()));
    });
    try {
      const row = await Promise.race([probe(t, localVersion, timeoutMs), deadline]);
      // A state.json left behind by a server that is gone: status.py says
      // so before asking, from its health check; this learns it by asking.
      if (!t.fingerprint && t.name === LOCAL && !row.reachable &&
          /^not reachable \((ECONNREFUSED|ECONNRESET|ENOTFOUND)\)$/.test(String(row.error))) {
        return { ...row, local: true, error: "not running here (quern start)" };
      }
      return row;
    } finally {
      clearTimeout(timer);
    }
  }));
  for (const row of rows) {
    // By position and label, never by name alone: a remote paired as "local"
    // is not this Mac.
    row.local = row.name === LOCAL && row.label === "this Mac";
  }
  // The local row is this Mac's only when it came from local(): a paired
  // remote can be called "local" only by editing remotes.json by hand.
  return { local_version: localVersion, remotes: rows };
}
