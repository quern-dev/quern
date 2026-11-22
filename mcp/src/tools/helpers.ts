import { z } from "zod";

import { connectionCode, isUnreachable, ServerRefused, ServerTimeout } from "../http.js";

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
  // A server that answered is never "unreachable", however a caller wrapped
  // its refusal -- checked first so the #471 invariant does not rest on a
  // refusal carrying no connection code (CodeRabbit, #472).
  if (e instanceof ServerRefused) return `${label}: ${message}`;
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

/**
 * `reveal` on the tools that show flows (#742). Secret headers and token-like
 * query parameters are redacted in what is shown: the server keeps the real
 * values and uses them for replays, mocks and released flows.
 */
export const revealParam = z
  .union([z.boolean(), z.array(z.string())])
  .optional()
  .describe(
    "Secret headers (authorization, proxy-authorization, cookie, set-cookie, x-api-key, " +
    "x-auth-token) and token-like query parameters (access_token, token, key, sig...) are " +
    "redacted in what is shown, as e.g. `Bearer ‹redacted, 1,184 chars, id 3f2a9c1e04bd›`. " +
    "Equal values share an id on this server, so you can tell whether a token changed " +
    "without seeing it. Pass names to see those values (e.g. [\"authorization\"]), or true " +
    "for all; the answer's `revealed` says what was shown. Bodies are not redacted. A " +
    "placeholder you pass back -- in release_flow, replay_flow or a mock -- is sent as the " +
    "real value it stands for.",
  );

/** `reveal` as the REST routes take it in a query string. */
export function revealQuery(reveal: boolean | string[] | undefined): string | undefined {
  if (reveal === undefined || reveal === false) return undefined;
  if (reveal === true) return "true";
  // A trailing comma keeps a list a list: the server reads a bare "true" as
  // "reveal everything", so a configured parameter named `true` asked for
  // alone would show every other secret (review).
  return reveal.length ? reveal.join(",") + "," : undefined;
}
