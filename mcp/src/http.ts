import { discoverServer } from "./config.js";

export async function apiRequest(
  method: "GET" | "POST" | "DELETE",
  path: string,
  params?: Record<string, string | number | boolean | undefined>,
  body?: unknown
): Promise<unknown> {
  const server = discoverServer();
  const url = new URL(path, server.url);

  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null) {
        url.searchParams.set(k, String(v));
      }
    }
  }

  const headers: Record<string, string> = {
    Authorization: `Bearer ${server.apiKey}`,
  };

  const init: RequestInit = { method, headers };

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
  const serverUrl = discoverServer().url;
  try {
    await fetch(new URL("/health", serverUrl).toString(), {
      signal: AbortSignal.timeout(3000),
    });
    console.error(`Connected to Quern Debug Server at ${serverUrl}`);
  } catch {
    console.error(
      `WARNING: Cannot reach Quern Debug Server at ${serverUrl} — use ensure_server tool to start it`
    );
  }
}
