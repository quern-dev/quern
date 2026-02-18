import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

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

export function discoverServer(): { url: string; apiKey: string } {
  // Priority 1: Environment variable
  if (process.env.QUERN_DEBUG_SERVER_URL) {
    return {
      url: process.env.QUERN_DEBUG_SERVER_URL,
      apiKey: loadApiKey(),
    };
  }

  // Priority 2: State file
  const state = readStateFile();
  if (state) {
    return {
      url: `http://127.0.0.1:${state.server_port}`,
      apiKey: state.api_key || loadApiKey(),
    };
  }

  // Priority 3: Default
  return {
    url: "http://127.0.0.1:9100",
    apiKey: loadApiKey(),
  };
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
