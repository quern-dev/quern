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

import { WebSocket as UndiciWebSocket } from "undici";
import { discoverServer } from "./config.js";
import { pinnedAgent } from "./remote.js";
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
/** Handshakes in a row that failed before the server answered. A refused API
 * key fails the handshake itself (HTTP 403), which reads as a plain drop, so
 * the 4401 close is never seen; retrying the same attempt then went on for
 * ever (review). After a few, the session is opened again from the start,
 * whose POST says what is wrong. */
let failedHandshakes = 0;
const REOPEN_AFTER_FAILED_HANDSHAKES = 3;
/** One immediate reopen after a 4404; later ones, before the session attaches
 * again, back off -- a repeated 4404 was a tight POST/WebSocket loop
 * (CodeRabbit). Reset on attach. */
let reopenedAtOnce = false;
/** Bumped when use_remote moves the session to another server: an open or a
 * connect begun for the old server must not attach this client there. */
let generation = 0;

/** Queue a notice unless the same one is already waiting to be delivered. */
function notice(text: string): void {
  if (!sessionNotices.includes(text)) sessionNotices.push(text);
}

const LOSS_NOTICE =
  "Your quern session ended on the server (it was released, the server restarted, " +
  "or its grace period ran out), so any devices you reserved are no longer yours. " +
  "Reserve them again with reserve_devices if you need them.";

/** Run when the session had to be opened afresh after a loss: what was loaded
 * for the old one -- its knowledge base (#560) -- is gone with it. */
let onReopened: (() => Promise<void>) | null = null;
export function whenReopened(fn: (() => Promise<void>) | null): void {
  onReopened = fn;
}

export type HeldState = "idle" | "opening" | "connecting" | "live" | "retrying" | "stopped";
let state: HeldState = "idle";

export function heldSessionState(): { id: string; state: HeldState } {
  return { id: SESSION_ID, state };
}

/** Open the session now, before its socket is held: what is loaded for the
 * session at start (#560) needs the session to exist. `startHeldSession`
 * then attaches with the token this got. False when it could not be opened;
 * the start opens it again as usual. */
export async function openHeldSession(): Promise<boolean> {
  const gen = generation;
  try {
    const opened = (await apiRequest("POST", "/api/v1/sessions", undefined, {
      name: SESSION_ID,
    }, 5000)) as { resume_token?: string } | null;
    // Opened on a server this client has since left (use_remote): its token
    // must not be presented to the new one (review).
    if (gen !== generation) return false;
    if (!opened?.resume_token) return false;
    token = opened.resume_token;
    return true;
  } catch {
    return false;
  }
}

/** Whether the session exists on the server now, waiting up to `timeoutMs`
 * for one being opened: a load made for this session (#560) needs it. Opens
 * it at once when nothing has started it yet. */
export async function sessionReady(timeoutMs = 5000): Promise<boolean> {
  // Live is attached. A token not yet attached is a session just opened --
  // opened early (idle) or on its first connect -- which exists. A reconnect
  // after an attach is not: after a server restart its token may name a
  // session that is gone, so it counts only once attached again (review).
  const exists = () => state === "live" ||
    (token !== null && !everAttached && (state === "idle" || state === "connecting"));
  if (exists()) return true;
  if (state === "idle" && !token) return openHeldSession();
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (exists()) return true;
    await new Promise((r) => setTimeout(r, 100));
  }
  return exists();
}

/** Open the session and hold its socket. Returns at once; retries quietly. */
export function startHeldSession(): void {
  if (typeof WebSocket === "undefined") {
    // Node 22 has a global WebSocket; anything older cannot hold a session.
    console.error("Held session unavailable: this Node has no WebSocket");
    return;
  }
  if (token) {
    connect();                // opened already, for the session's knowledge base
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
  const gen = generation;
  state = "opening";
  try {
    const opened = (await apiRequest("POST", "/api/v1/sessions", undefined, {
      name: SESSION_ID,
    })) as { resume_token?: string; resumed?: boolean } | null;
    if (gen !== generation) return;   // moved to another server meanwhile
    if (!opened?.resume_token) throw new Error("no resume token in the answer");
    token = opened.resume_token;
    // A client that has held a session before and is handed a *new* one has
    // lost what it held -- whichever way it got here: a 4404, or reopening
    // after failed handshakes while the server was down, which never saw a
    // 4404 at all (found live, after that path was added).
    if (everAttached && opened.resumed === false) {
      notice(LOSS_NOTICE);
      // The session exists again (it was just opened), so what was loaded
      // for the old one can be loaded for this one. Never awaited here, and
      // never allowed to throw into the reconnect.
      if (onReopened) void onReopened().catch(() => {});
    }
    connect();
  } catch {
    if (gen === generation) retry(() => void open());
  }
}

/** Move the held session to the server discoverServer() names now (use_remote):
 * the old socket is closed cleanly -- which releases anything still held there
 * -- and a session under the same id is opened on the new server and held.
 * True once it is live there; false if it is not within `timeoutMs`, or the
 * client is exiting. */
export async function switchHeldSession(timeoutMs = 10_000): Promise<boolean> {
  if (stopping) return false;   // exiting: open nothing new anywhere (review)
  generation += 1;
  if (timer) clearTimeout(timer);
  const old = socket;
  socket = null;              // its end is no longer the current attempt's
  try {
    old?.close(1000, "switching server");
  } catch {
    // already closed
  }
  token = null;
  everAttached = false;       // a new server's fresh session is not a loss
  failedHandshakes = 0;
  reopenedAtOnce = false;
  backoffMs = 1000;
  state = "idle";
  // What was said about, and loaded for, the old server's session does not
  // carry over: a notice would read as being about the new one, and the
  // knowledge-base reload would upload to it (review).
  sessionNotices.length = 0;
  onReopened = null;
  await openHeldSession();
  startHeldSession();
  // Read through a call: TypeScript narrows `state` to "idle" from the
  // assignment above, not knowing the socket's handlers change it meanwhile.
  const live = () => heldSessionState().state === "live";
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline && !live() && !stopping) {
    await new Promise((r) => setTimeout(r, 100));
  }
  return live();
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
    // A remote quern's socket is pinned like its requests (#254, phase 3):
    // undici's WebSocket takes the pinned dispatcher; Node's global one has
    // no way to.
    ws = server.fingerprint
      ? (new UndiciWebSocket(url, {
          headers: { ...quernHeaders(server.apiKey), "X-Quern-Resume-Token": token },
          dispatcher: pinnedAgent(server.fingerprint),
        }) as unknown as WebSocket)
      : new WebSocket(url, init);
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
  let attached = false;
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
    if (!current) return;
    if (!attached && code === 1006) failedHandshakes += 1;
    onEnd(code);
  };

  ws.onmessage = (event) => {
    try {
      const message = JSON.parse(String(event.data)) as { event?: string };
      if (message.event === "released") {
        const freed = (message as { udids?: string[]; by?: string }).udids ?? [];
        const by = (message as { by?: string }).by ?? "another caller";
        notice(
          `${by} released ${freed.join(", ")} from your quern session; ` +
            "it is no longer reserved for you. Reserve it again if you still need it.",
        );
      }
      if (message.event === "attached") {
        attached = true;
        failedHandshakes = 0;
        reopenedAtOnce = false;
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
    // At once the first time: the server is up (it just answered), and every
    // second here is a second the agent holds nothing. Again before attaching
    // means something is wrong: back off.
    if (!reopenedAtOnce) {
      reopenedAtOnce = true;
      backoffMs = 1000;
      void open();
    } else {
      retry(() => void open());
    }
  } else if (code === CLOSE_BAD_TOKEN || code === CLOSE_UNAUTHORISED) {
    // A new token by name (local takeover), or a rotated API key: start over.
    token = null;
    retry(() => void open());
  } else if (failedHandshakes >= REOPEN_AFTER_FAILED_HANDSHAKES) {
    // The handshake keeps failing: open the session again from the start.
    failedHandshakes = 0;
    token = null;
    retry(() => void open());
  } else {
    // Dropped: reconnect with the token while the server holds the session.
    retry(connect);
  }
}
