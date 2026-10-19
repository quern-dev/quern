import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { loadRemote } from "./remote.js";

export const CONFIG_DIR = join(homedir(), ".quern");
export const STATE_FILE = join(CONFIG_DIR, "state.json");
export const API_KEY_FILE = join(CONFIG_DIR, "api-key");

export interface ServerState {
  pid: number;
  server_port: number;
  proxy_port: number;
  proxy_enabled: boolean;
  proxy_status: string;
  started_at: string;
  api_key: string;
  active_devices: string[];
}

export function readStateFile(): ServerState | null {
  try {
    if (!existsSync(STATE_FILE)) return null;
    const content = readFileSync(STATE_FILE, "utf-8").trim();
    if (!content) return null;
    return JSON.parse(content) as ServerState;
  } catch {
    return null;
  }
}

/** The port quern uses when nothing says otherwise.
 *
 * Mirrors DEFAULT_SERVER_PORT in server/lifecycle/ports.py, which is the
 * definition; tests/test_mcp_defaults.py fails if the two drift. It is a last
 * resort and nothing more -- the running server's port comes from
 * ~/.quern/state.json, and is not this one whenever something else had it
 * first.
 */
export const DEFAULT_SERVER_PORT = 9110;

/** Where the URL came from, so a caller can tell "unreachable" from
 * "nothing told us where to look". */
export type ServerSource = "remote" | "env" | "state" | "default";

export function discoverServer(): {
  url: string;
  apiKey: string;
  source: ServerSource;
  /** Set for a remote quern: every connection is pinned to it (remote.ts). */
  fingerprint?: string;
} {
  // Priority 0: a paired remote quern (#254, phase 3). Named explicitly, so
  // it wins over everything -- and if it cannot be read this throws rather
  // than falling back: quietly driving this Mac's server instead of the one
  // asked for would put an agent's work on the wrong machine.
  const remoteName = process.env.QUERN_REMOTE?.trim();
  if (remoteName) {
    const remote = loadRemote(remoteName);
    return { url: remote.url, apiKey: remote.key, source: "remote",
             fingerprint: remote.fingerprint };
  }

  // Priority 1: Environment variable.
  //
  // QUERN_DEBUG_SERVER_URL is the prototype's name and is deprecated. It is
  // still honoured, because someone has it exported in a shell profile and a
  // silent change of meaning is worse than a rename, but it warns -- and the
  // warning goes to stderr, because stdout is the JSON-RPC channel.
  const url = process.env.QUERN_SERVER_URL ?? process.env.QUERN_DEBUG_SERVER_URL;
  if (url) {
    if (!process.env.QUERN_SERVER_URL) {
      console.error(
        "QUERN_DEBUG_SERVER_URL is deprecated; use QUERN_SERVER_URL. " +
          "`eval \"$(quern env)\"` sets it for you.",
      );
    }
    return { url, apiKey: explicitKey() ?? loadApiKey(), source: "env" };
  }

  // Priority 2: State file
  const state = readStateFile();
  if (state) {
    return {
      url: `http://127.0.0.1:${state.server_port}`,
      apiKey: explicitKey() ?? (state.api_key || loadApiKey()),
      source: "state",
    };
  }

  // Priority 3: a guess, flagged as one. Reaching here means no server has
  // published itself, so every request will fail whatever URL we return --
  // what matters is that callers can say "nothing is running" instead of
  // naming a port the server may never have been on.
  return {
    url: `http://127.0.0.1:${DEFAULT_SERVER_PORT}`,
    apiKey: explicitKey() ?? loadApiKey(),
    source: "default",
  };
}

/** QUERN_CLIENT_KEY: a client key from `quern clients add` (#254, phase 2),
 * which wins over the owner's key in state.json and ~/.quern/api-key. Unset or
 * blank means "use the owner's". Not QUERN_API_KEY, which `quern env` exports
 * with the owner's key: preferring that would pin an MCP to a key since
 * regenerated. */
function explicitKey(): string | undefined {
  const key = process.env.QUERN_CLIENT_KEY?.trim();
  return key ? key : undefined;
}

function loadApiKey(): string {
  try {
    return readFileSync(API_KEY_FILE, "utf-8").trim();
  } catch {
    console.error(
      "WARNING: Could not read API key from ~/.quern/api-key"
    );
    return "";
  }
}
