import { discoverServer } from "./config.js";

export async function apiRequest(
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  params?: Record<string, string | number | boolean | string[] | undefined>,
  body?: unknown,
  timeoutMs?: number
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

  if (timeoutMs) {
    init.signal = AbortSignal.timeout(timeoutMs);
  }

  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  const resp = await fetch(url.toString(), init);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`HTTP ${resp.status}: ${text}`);
  }

  const text = await resp.text();
  if (!text) return null;
  return JSON.parse(text);
}

export async function probeServer(): Promise<void> {
  const server = discoverServer();
  try {
    await fetch(new URL("/health", server.url).toString(), {
      signal: AbortSignal.timeout(3000),
    });
    console.error(`Connected to Quern at ${server.url}`);
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
  }
}
