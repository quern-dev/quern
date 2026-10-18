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
 * travels as `session_notices` on the next API response (see http.ts).
 */

import { discoverServer } from "./config.js";
import { SESSION_ID, apiRequest, quernHeaders, sessionNotices } from "./http.js";

/** The server's close codes (server/api/sessions.py). */
const CLOSE_UNKNOWN = 4404;
const CLOSE_BAD_TOKEN = 4403;
const CLOSE_UNAUTHORISED = 4401;

const MAX_BACKOFF_MS = 30_000;

let token: string | null = null;
let socket: WebSocket | null = null;
let stopping = false;
let backoffMs = 1000;
let timer: ReturnType<typeof setTimeout> | null = null;
let everAttached = false;

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

  ws.onmessage = (event) => {
    try {
      const message = JSON.parse(String(event.data)) as { event?: string };
      if (message.event === "attached") {
        state = "live";
        backoffMs = 1000;
        everAttached = true;
      }
    } catch {
      // not JSON: nothing this client acts on
    }
  };

  ws.onclose = (event) => {
    if (socket === ws) socket = null;
    if (stopping) return;
    if (event.code === CLOSE_UNKNOWN) {
      token = null;
      if (everAttached) {
        sessionNotices.push(
          "Your quern session ended on the server (it restarted, or its grace period " +
            "ran out), so any devices you reserved were released. Reserve them again " +
            "with reserve_devices if you need them.",
        );
      }
      retry(() => void open());
    } else if (event.code === CLOSE_BAD_TOKEN || event.code === CLOSE_UNAUTHORISED) {
      // A new token by name (local takeover), or a rotated API key: start over.
      token = null;
      retry(() => void open());
    } else {
      // Dropped: reconnect with the token while the server holds the session.
      retry(connect);
    }
  };

  ws.onerror = () => {
    // onclose follows and decides what to do.
  };
}
