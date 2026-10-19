/** A remote quern, reached over TLS pinned to its certificate (#254, phase 3).
 *
 * `quern connect` pairs this machine with a server and records it in
 * ~/.quern/remotes.json under a name. `QUERN_REMOTE=<name>` points this MCP at
 * it: every request, and the held-session WebSocket, goes through an undici
 * dispatcher whose connector checks the presented certificate's SHA-256
 * against the recorded fingerprint *before* the connection is handed to the
 * request -- so a mismatch sends nothing, not even the key in the headers.
 * Node's own https only lets a check run after the handshake, by which time a
 * request may already be on its way.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { TLSSocket } from "node:tls";
import { Agent, buildConnector } from "undici";

// Not config.ts's CONFIG_DIR: config.ts imports this module, and reading its
// constant while the two are still loading is a temporal-dead-zone error.
export const REMOTES_FILE = join(homedir(), ".quern", "remotes.json");

export interface RemoteServer {
  name: string;
  url: string;
  key: string;
  fingerprint: string; // sha256 of the DER certificate, lowercase hex
}

/** The fingerprint a person reads: `SHA256:AB:CD:...`. */
export function showFingerprint(hex: string): string {
  return "SHA256:" + (hex.match(/../g) ?? []).map((p) => p.toUpperCase()).join(":");
}

function normalise(value: string): string {
  let v = value.trim();
  if (v.toLowerCase().startsWith("sha256:")) v = v.slice(7);
  v = v.replaceAll(":", "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(v)) throw new Error(`not a SHA-256 fingerprint: ${value}`);
  return v;
}

/** The remote `name` names in remotes.json. Throws, saying what to do, when
 * it cannot be used -- never falls back to the local server, which would send
 * an agent's work to a different machine than the one it asked for. */
export function loadRemote(name: string, file = REMOTES_FILE): RemoteServer {
  let data: unknown;
  try {
    data = JSON.parse(readFileSync(file, "utf-8"));
  } catch (e) {
    throw new Error(
      `QUERN_REMOTE=${name}, but ${file} could not be read (${(e as Error).message}): ` +
        `pair with it first -- quern connect <url> --code <code> --name ${name}`,
    );
  }
  const entry = (data as { remotes?: Record<string, unknown> })?.remotes?.[name] as
    | { url?: unknown; key?: unknown; fingerprint?: unknown }
    | undefined;
  if (!entry || typeof entry.url !== "string" || typeof entry.key !== "string" ||
      typeof entry.fingerprint !== "string") {
    throw new Error(
      `QUERN_REMOTE=${name}, but ${file} has no complete remote by that name: ` +
        `pair with it -- quern connect <url> --code <code> --name ${name}`,
    );
  }
  return { name, url: entry.url.replace(/\/+$/, ""), key: entry.key,
           fingerprint: normalise(entry.fingerprint) };
}

export class PinMismatch extends Error {
  constructor(readonly expected: string, readonly presented: string) {
    super(
      `the server's certificate is not the one paired with: expected ` +
        `${showFingerprint(expected)}, presented ${showFingerprint(presented)}. Its ` +
        "certificate was rotated (pair again: quern connect), or something is between " +
        "you and it",
    );
    this.name = "PinMismatch";
  }
}

const agents = new Map<string, Agent>();

/** An undici dispatcher whose every connection is pinned to `fingerprint`.
 * `timeouts: false` drops undici's own header and body timeouts, for
 * requests -- builds -- that must wait as long as the server takes. */
export function pinnedAgent(fingerprint: string, { timeouts = true } = {}): Agent {
  const cacheKey = `${fingerprint}:${timeouts}`;
  const cached = agents.get(cacheKey);
  if (cached) return cached;
  const base = buildConnector({ rejectUnauthorized: false });
  const agent = new Agent({
    ...(timeouts ? {} : { headersTimeout: 0, bodyTimeout: 0 }),
    connect: (options, callback) => {
      base(options, (err, socket) => {
        if (err || !socket) {
          callback(err ?? new Error("no connection"), null);
          return;
        }
        const der = (socket as TLSSocket).getPeerCertificate?.(true)?.raw;
        const presented = der ? createHash("sha256").update(der).digest("hex") : "";
        if (presented !== fingerprint) {
          socket.destroy();
          callback(new PinMismatch(fingerprint, presented || "(none)"), null);
          return;
        }
        callback(null, socket);
      });
    },
  });
  agents.set(cacheKey, agent);
  return agent;
}
