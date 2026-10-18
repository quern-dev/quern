import { z } from "zod";

import { isUnreachable, ServerTimeout } from "../http.js";

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
export function errorText(e: unknown): string {
  const message = e instanceof Error ? e.message : String(e);
  if (isUnreachable(e)) {
    return `Error: ${message}\n\nIs the Quern server running? Start it with ` +
      "`quern start`, or call ensure_server.";
  }
  if (e instanceof ServerTimeout) {
    return `Error: ${message}. It may be busy with a long read or a build; ` +
      "retry, or check it with ensure_server if it keeps happening.";
  }
  return `Error: ${message}`;
}
