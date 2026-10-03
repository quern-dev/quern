import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";

import { discoverServer } from "./config.js";

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
