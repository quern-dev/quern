import { z } from "zod";

import { connectionCode, isUnreachable, ServerTimeout } from "../http.js";

/**
 * Wraps a raw zod shape with z.object().strict() so unknown keys are rejected.
 * Used with server.registerTool()'s inputSchema property.
 */
export function strictParams<T extends z.ZodRawShape>(shape: T) {
  return z.object(shape).strict();
}

/** The text of a failed tool call, the same from every tool (#471).
 *
 * A server that answered is quoted as it answered: its status and its body,
 * which says why. Only a connection that was never made gets the hint to start
 * the server -- appended to a 403 it told an agent the opposite of what had
 * happened, and sent it to start a second server instead of reading the
 * refusal. */
export function errorText(e: unknown, label = "Error"): string {
  const message = e instanceof Error ? e.message : String(e);
  if (isUnreachable(e)) {
    return `${label}: ${message}\n\nIs the Quern server running? Start it with ` +
      "`quern start`, or call ensure_server.";
  }
  if (e instanceof ServerTimeout) {
    return `${label}: ${message}. It may be busy with a long read or a build ` +
      "(or, on another machine, unreachable); retry, or check it with ensure_server " +
      "if it keeps happening.";
  }
  // Node's fetch says only "fetch failed" and puts the reason on `cause`: a
  // reset when the server crashed or restarted mid-request reads as nothing at
  // all without it (#471 review). The request may have run, so no hint to
  // start a server -- just what happened.
  const cause = (e as { cause?: unknown })?.cause;
  const code = connectionCode(e);
  const causeMessage = cause instanceof Error ? cause.message : "";
  const detail = [code, causeMessage].filter((x) => x && !message.includes(x)).join(": ");
  return detail ? `${label}: ${message} (${detail})` : `${label}: ${message}`;
}
