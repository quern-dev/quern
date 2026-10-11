import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";

import { randomBytes } from "node:crypto";
import { discoverServer } from "./config.js";

/** The server's rule for a session id (server/session.py).
 *
 * Declared before SESSION_ID, which reads it while the module loads. Below it,
 * it is in its temporal dead zone at that moment: with QUERN_SESSION set, the
 * MCP server threw on startup and never served a tool. Without it the pattern
 * is never read, so only a run with the variable set could see it -- found by
 * the live two-agent test, not the unit tests. */
export const SESSION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,63}$/;

/** Which agent session this MCP process serves, sent as X-Quern-Session on
 * every request so the server can say who asked for each action (#254).
 *
 * Measured to be one MCP process per agent session: four Claude Code sessions
 * on one machine ran four of these, each the child of its own session. So
 * even the random fallback lives exactly as long as the session, with no
 * cooperation needed from the agent.
 *
 * Under Claude Code it is the agent's own session id, which names its
 * transcript, so a reader can open the conversation behind an action -- and
 * the same id reaches the agent's Bash commands, so the CLI attributes them
 * the same way. Another MCP client gets a random id. QUERN_SESSION overrides
 * both, for a caller that wants a name it chose. Attribution, not authority. */
export const SESSION_ID: string = mintSessionId(
  process.env.QUERN_SESSION, process.env.CLAUDE_CODE_SESSION_ID);

/** The rule every quern client shares (server/session.py session_from_env):
 * QUERN_SESSION if valid; else `claude-<id>` under Claude Code, whose session
 * id names the agent's transcript in ~/.claude/projects and is also in the
 * environment of the agent's Bash commands, so CLI calls the agent makes land
 * under the same session; else a random id for this process. */
export function mintSessionId(
  override: string | undefined, claudeSession?: string | undefined,
): string {
  const named = override?.trim();
  if (named && SESSION_ID_PATTERN.test(named)) return named;
  const claude = `claude-${claudeSession?.trim() ?? ""}`;
  if (claudeSession?.trim() && SESSION_ID_PATTERN.test(claude)) return claude;
  return `mcp-${randomBytes(4).toString("hex")}`;
}

/** One request with no client-side timeout at all.
 *
 * `fetch` cannot make one. Its HTTP client gives up on a response whose
 * headers have not arrived in 300s, whatever signal it is passed, and a
 * Gradle or Xcode build answers only when it is done -- so a build that took
 * six minutes and succeeded read as a failed tool call. The server bounds
 * its own builds; the client waiting for them must not add a second, shorter
 * limit of its own.
 */
export function requestWithoutTimeout(
  url: URL,
  method: string,
  headers: Record<string, string>,
  body?: string
): Promise<{ status: number; text: string }> {
  const send = url.protocol === "https:" ? httpsRequest : httpRequest;
  return new Promise((resolve, reject) => {
    const req = send(url, { method, headers }, (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (c: Buffer) => chunks.push(c));
      res.on("end", () =>
        resolve({ status: res.statusCode ?? 0, text: Buffer.concat(chunks).toString("utf8") })
      );
      res.on("error", reject);
    });
    req.on("error", reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

export async function apiRequest(
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  params?: Record<string, string | number | boolean | string[] | undefined>,
  body?: unknown,
  /** Milliseconds, or "none" for a request that may legitimately take longer
   * than fetch's built-in 300s wait for headers: a build. */
  timeoutMs?: number | "none"
): Promise<unknown> {
  const server = discoverServer();
  const url = new URL(path, server.url);

  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null) {
        if (Array.isArray(v)) {
          for (const item of v) url.searchParams.append(k, String(item));
        } else {
          url.searchParams.set(k, String(v));
        }
      }
    }
  }

  const headers: Record<string, string> = {
    Authorization: `Bearer ${server.apiKey}`,
    "X-Quern-Session": SESSION_ID,
  };

  const init: RequestInit = { method, headers };

  if (typeof timeoutMs === "number" && timeoutMs) {
    init.signal = AbortSignal.timeout(timeoutMs);
  }

  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  let status: number;
  let text: string;
  if (timeoutMs === "none") {
    ({ status, text } = await requestWithoutTimeout(
      url, method, headers, init.body as string | undefined));
  } else {
    const resp = await fetch(url.toString(), init);
    status = resp.status;
    text = await resp.text();
  }
  if (status < 200 || status > 299) {
    throw new Error(`HTTP ${status}: ${text}`);
  }

  if (!text) return null;
  return JSON.parse(text);
}

/** Whether the server answered. Any answer counts: the question is whether
 * anything is listening, and a later request reports its own status. */
export async function probeServer(): Promise<boolean> {
  const server = discoverServer();
  try {
    await fetch(new URL("/health", server.url).toString(), {
      signal: AbortSignal.timeout(3000),
    });
    console.error(`Connected to Quern at ${server.url}`);
    return true;
  } catch {
    if (server.source === "default") {
      // Naming the guessed URL here read as "your server at 9100 is down",
      // which sent people to look at a port the server may never have used.
      console.error(
        "WARNING: No running Quern found (~/.quern/state.json is missing) — " +
          "use the ensure_server tool to start it"
      );
    } else {
      console.error(
        `WARNING: Cannot reach Quern at ${server.url} — use ensure_server tool to start it`
      );
    }
    return false;
  }
}
