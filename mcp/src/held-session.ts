/** This MCP process's held session on the quern server (#254, phase 1).
 *
 * One MCP process serves one agent session, so it opens a session under the
 * same id it already sends as X-Quern-Session, and holds the session's live
 * WebSocket for as long as it runs. The socket is what reservations stand on:
 *
 *   - a clean close (this process exiting) releases the agent's devices at once;
 *   - a dropped connection keeps them for the server's grace period, and this
 *     reconnects with the resume token to reclaim them;
 *   - "unknown session" (close 4404) means the server restarted or the grace
 *     ran out: the devices are gone, so it opens a fresh session and tells the
 *     agent, on its next tool response, to reserve again.
 *
 * Never throws and never blocks startup: a server that is down is retried in
 * the background. Agents cannot receive pushes, so what they need to know
 * travels on its next tool result (see notices.ts).
 */

import { discoverServer } from "./config.js";
import { SESSION_ID, apiRequest, quernHeaders, sessionNotices } from "./http.js";

/** The server's close codes (server/api/sessions.py). */
const CLOSE_UNKNOWN = 4404;
const CLOSE_BAD_TOKEN = 4403;
const CLOSE_UNAUTHORISED = 4401;
const CLOSE_REPLACED = 4000;

/** Capped low on purpose: reconnecting has to land well inside the server's
 * grace period (120 s at the least), and after a server restart a long
 * backoff left the agent unheld, and unaware its devices were gone, for as
 * long as the backoff had grown while the server was down (found live). */
const MAX_BACKOFF_MS = 5_000;
/** A handshake that has neither opened nor failed by now is given up on. */
const HANDSHAKE_TIMEOUT_MS = 10_000;

let token: string | null = null;
let socket: WebSocket | null = null;
let stopping = false;
let backoffMs = 1000;
let timer: ReturnType<typeof setTimeout> | null = null;
let everAttached = false;
/** Set when the server said the session is gone; the agent is told once the
 * session is reopened. Not queued before: the reopen is itself an API request,
 * and a notice queued first rode on that answer, which this module reads and
 * the agent never sees (found live). */
let lossPending = false;

const LOSS_NOTICE =
  "Your quern session ended on the server (it was released, the server restarted, " +
  "or its grace period ran out), so any devices you reserved are no longer yours. " +
  "Reserve them again with reserve_devices if you need them.";

export type HeldState = "idle" | "opening" | "connecting" | "live" | "retrying" | "stopped";
let state: HeldState = "idle";

export function heldSessionState(): { id: string; state: HeldState } {
  return { id: SESSION_ID, state };
}

/** Open the session and hold its socket. Returns at once; retries quietly. */
export function startHeldSession(): void {
  if (typeof WebSocket === "undefined") {
    // Node 22 has a global WebSocket; anything older cannot hold a session.
    console.error("Held session unavailable: this Node has no WebSocket");
    return;
  }
  void open();
}

/** Close the socket cleanly, which releases the agent's devices at once. */
export function stopHeldSession(): void {
  stopping = true;
  state = "stopped";
  if (timer) clearTimeout(timer);
  try {
    socket?.close(1000, "mcp exiting");
  } catch {
    // already closed
  }
}

function retry(step: () => void): void {
  if (stopping) return;
  state = "retrying";
  if (timer) clearTimeout(timer);
  timer = setTimeout(step, backoffMs);
  // Never keeps the process alive on its own.
  (timer as { unref?: () => void }).unref?.();
  backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
}

async function open(): Promise<void> {
  if (stopping) return;
  state = "opening";
  try {
    const opened = (await apiRequest("POST", "/api/v1/sessions", undefined, {
      name: SESSION_ID,
    })) as { resume_token?: string } | null;
    if (!opened?.resume_token) throw new Error("no resume token in the answer");
    token = opened.resume_token;
    if (lossPending) {
      lossPending = false;
      sessionNotices.push(LOSS_NOTICE);
    }
    connect();
  } catch {
    retry(() => void open());
  }
}

function connect(): void {
  if (stopping || !token) return;
  state = "connecting";
  let server;
  try {
    server = discoverServer();
  } catch {
    retry(() => void open());
    return;
  }
  const url = new URL(`/api/v1/sessions/${SESSION_ID}/live`, server.url);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  // Node's WebSocket (undici) takes headers in its init object, which the
  // DOM type does not declare.
  const init = {
    headers: { ...quernHeaders(server.apiKey), "X-Quern-Resume-Token": token },
  } as unknown as string[];
  let ws: WebSocket;
  try {
    ws = new WebSocket(url, init);
  } catch {
    retry(connect);
    return;
  }
  socket = ws;

  // One end per attempt. Node 22's WebSocket (undici) fires only `error`, never
  // `close`, when the connection is refused -- readyState stays CONNECTING for
  // ever -- so waiting for `close` left a client whose server had restarted
  // retrying nothing, unheld, and with no notice for its agent (found live).
  let done = false;
  const handshake = setTimeout(() => ended(1006), HANDSHAKE_TIMEOUT_MS);
  (handshake as { unref?: () => void }).unref?.();
  const ended = (code: number) => {
    if (done) return;
    done = true;
    clearTimeout(handshake);
    const current = socket === ws;
    if (current) socket = null;
    try {
      ws.close();
    } catch {
      // never opened
    }
    // Only the current attempt decides what happens next: a superseded one
    // ending must not start another reconnect alongside the live one.
    if (current) onEnd(code);
  };

  ws.onmessage = (event) => {
    try {
      const message = JSON.parse(String(event.data)) as { event?: string };
      if (message.event === "attached") {
        clearTimeout(handshake);
        state = "live";
        backoffMs = 1000;
        everAttached = true;
      }
    } catch {
      // not JSON: nothing this client acts on
    }
  };

  ws.onclose = (event) => ended(event.code);
  ws.onerror = () => {
    // After an open socket, `close` follows and carries the code. Before one,
    // it may never come: treat a failed handshake as a dropped connection.
    if (ws.readyState === WebSocket.CONNECTING) ended(1006);
  };
}

/** What to do when an attempt ends, by the code it ended with. */
function onEnd(code: number): void {
  if (stopping) return;
  console.error(`Held session ${SESSION_ID}: connection ended (code ${code})`);
  if (code === CLOSE_REPLACED) {
    // Another connection presented this session's token and took it over.
    // Reconnecting would only take it back; leave it to the newer holder.
    state = "stopped";
    console.error(`Held session ${SESSION_ID}: taken over by another connection; not reconnecting`);
  } else if (code === CLOSE_UNKNOWN) {
    token = null;
    if (everAttached) lossPending = true;
    // At once: the server is up (it just answered), and every second here
    // is a second the agent holds nothing.
    backoffMs = 1000;
    void open();
  } else if (code === CLOSE_BAD_TOKEN || code === CLOSE_UNAUTHORISED) {
    // A new token by name (local takeover), or a rotated API key: start over.
    token = null;
    retry(() => void open());
  } else {
    // Dropped: reconnect with the token while the server holds the session.
    retry(connect);
  }
}
