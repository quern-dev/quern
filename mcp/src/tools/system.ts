import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { LOCAL, currentRemote, discoverServer, selectServer, selectedServer } from "../config.js";
import { switchHeldSession } from "../held-session.js";
import { SESSION_ID, apiRequest, serverFetch } from "../http.js";
import { loadRemote } from "../remote.js";
import { errorText, strictParams } from "./helpers.js";
import { execFile } from "node:child_process";
import { homedir } from "node:os";

export function registerSystemTools(server: McpServer): void {
  server.registerTool(
    "update_quern",
    {
      description:
        `Update Quern to the latest release on the user's configured channel (stable or beta). Launches the equivalent of "quern update" — pulls the latest source (or fetches the release tarball), reinstalls Python deps, rebuilds the MCP server, and restarts the daemon. The HTTP call returns immediately; the actual upgrade runs in a detached child and takes ~30-60 seconds. The Quern server will restart during that window, so the next MCP tool call may fail until it's back up. Reconnect by retrying ensure_server. Only call this after the user has confirmed they want to update — typically prompted by an "update_available" hint in ensure_server's response.`,
      inputSchema: strictParams({}),
    },
    async () => {
      try {
        const data = await apiRequest("POST", "/api/v1/system/update");
        return {
          content: [
            { type: "text" as const, text: JSON.stringify(data, null, 2) },
          ],
        };
      } catch (e) {
        return {
          content: [
            {
              type: "text" as const,
              text: `${errorText(e, "Error launching update")}\n\nFall back to running "quern update" in a terminal manually.`,
            },
          ],
          isError: true,
        };
      }
    },
  );

  server.registerTool(
    "set_update_channel",
    {
      description:
        `Set the user's Quern update channel preference (stable or beta). "stable" tracks the release/stable branch (only tagged releases); "beta" tracks release/beta for opt-in early testing. Setting the channel only updates ~/.quern/config.json — it does not switch git branches or apply an update. After flipping to beta, the next call to update_quern (and any future periodic check) will compare against the beta branch. Only call this after the user has explicitly asked to change channels.`,
      inputSchema: strictParams({
        channel: z.enum(["stable", "beta"]).describe(
          "stable (default, recommended) or beta (opt-in prerelease)",
        ),
      }),
    },
    async ({ channel }) => {
      try {
        const data = await apiRequest(
          "PUT",
          "/api/v1/system/channel",
          undefined,
          { channel },
        );
        return {
          content: [
            { type: "text" as const, text: JSON.stringify(data, null, 2) },
          ],
        };
      } catch (e) {
        return {
          content: [
            {
              type: "text" as const,
              text: errorText(e, "Error setting channel"),
            },
          ],
          isError: true,
        };
      }
    },
  );

  server.registerTool("list_remotes", {
    description: `List the quern servers this agent could use -- this Mac's own (first, "local": true) and the remote ones this machine is paired with (quern connect) -- each asked now: whether it answers, its quern version (and whether it differs from this Mac's), and its devices -- each with name, device_family, os_version (e.g. "iOS 17.5"), device_type, state, and model where the remote reports one -- with which are free (not held by anyone), which this key holds (yours) and who holds the rest (held_by).

The entry marked "current": true is the server this MCP talks to. Its devices are the ones you can use now: reserve one with reserve_devices(os_version="18.6", device_family="iPad", name=<name or model>), or by udid. To use a device on another server, switch to it first with use_remote(name=<its name>), then reserve it.

A server that could not be asked says why (not running, timed out, certificate changed, key refused), never "no devices". Read-only; it uses only what each server already lets this key read.`,
    inputSchema: strictParams({}),
  }, async () => {
    try {
      const data = markCurrent(await quernRemoteList(), currentRemote() ?? LOCAL);
      return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) {
      return { content: [{ type: "text" as const, text: errorText(e) }], isError: true };
    }
  });

  server.registerTool("use_remote", {
    description: `Switch this session to another quern server -- a paired remote from list_remotes, or "local" for this Mac's own -- so every later tool call (reserve_devices, take_screenshot, tap_element, ...) works on that machine and its devices. Use it when the device the user wants is on another server.

One server at a time: devices you hold on the current server must be released first (release_devices), or pass release=true to release them as part of the switch -- also needed when the current server cannot say what you hold. The switch is made only once your session is live on the new server; if it cannot be, you stay where you were. Nothing else follows you -- mocks, proxy settings and loaded landmarks stay where they were set. The quern CLI in your shell is not switched; it still talks to this Mac's server.`,
    inputSchema: strictParams({
      name: z.string().describe(`A server's name from list_remotes, or "local" for this Mac's own`),
      release: z.boolean().optional().describe("Release the devices you hold on the current server as part of the switch (default false: refuse)"),
    }),
  }, (args) => {
    // One switch at a time: two at once each restored the other's "before",
    // leaving requests on one server and the held session on another (review).
    const run = switching.then(() => useRemote(args));
    switching = run.catch(() => undefined);
    return run;
  });
}

let switching: Promise<unknown> = Promise.resolve();

function reply(data: unknown, isError = false) {
  return {
    content: [{ type: "text" as const, text: typeof data === "string" ? data : JSON.stringify(data, null, 2) }],
    ...(isError ? { isError: true } : {}),
  };
}

async function useRemote({ name, release }: { name: string; release?: boolean }) {
  const target = name.trim();
  const from = currentRemote() ?? LOCAL;
  if (target === from) return reply({ current: target, note: "already using this server" });
  if (target !== LOCAL) {
    try {
      loadRemote(target);
    } catch {
      return reply(`No paired remote named "${target}": list_remotes shows the servers ` +
        `this machine is paired with, and "local" is this Mac's own.`, true);
    }
  }
  // Is the new server there? Asked of it directly, before anything is
  // switched: selecting it first sent every concurrent tool call there, even
  // when the switch was then abandoned (review).
  let url: string;
  let version: unknown;
  try {
    const server = discoverServer(target === LOCAL ? "" : target);
    url = server.url;
    const resp = await serverFetch(server, new URL("/health", server.url).toString(),
      { signal: AbortSignal.timeout(5000) });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    version = ((await resp.json()) as { version?: unknown }).version;
  } catch (e) {
    return reply(`${target} did not answer (${errorText(e)}); still using ${from}.`, true);
  }
  // What this session holds where it is now: switching would strand it, so
  // it is released only when asked to. "Could not ask" is not "nothing" -- a
  // clean close would release whatever is held without saying so (review).
  const sessionPath = `/api/v1/sessions/${encodeURIComponent(SESSION_ID)}`;
  let held: string[] | null;
  let unknown = "";
  try {
    // Bounded: a current server that has hung is when a switch is most
    // needed, and switches queue behind this one (CodeRabbit).
    const session = (await apiRequest("GET", sessionPath, undefined, undefined, 5000)) as
      { reservations?: { udid?: string }[] } | null;
    held = (session?.reservations ?? []).map((r) => String(r.udid));
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    if (message.startsWith("HTTP 404")) {
      held = [];                // no session there: nothing held
    } else {
      held = null;
      unknown = message;
    }
  }
  if (!release && held === null) {
    return reply(`Could not tell whether you hold devices on ${from} (${unknown}), and ` +
      `switching releases anything you hold there. Call use_remote(name="${target}", ` +
      `release=true) to switch anyway.`, true);
  }
  if (!release && held?.length) {
    return reply(`You hold ${held.join(", ")} on ${from}. Release them first ` +
      `(release_devices), or call use_remote(name="${target}", release=true).`, true);
  }
  let released: string[] = [];
  let releaseNote = "";
  if (release && (held === null || held.length)) {
    try {
      const freed = (await apiRequest("DELETE", `${sessionPath}/reservations`, undefined,
        undefined, 5000)) as
        { freed?: string[] } | null;
      released = freed?.freed ?? held ?? [];
    } catch (e) {
      releaseNote = `Could not release on ${from} (${errorText(e)}): anything held there is ` +
        "freed when its connection closes or its grace period ends.";
    }
  }
  const before = selectedServer();
  selectServer(target);
  if (!(await switchHeldSession())) {
    // Requests would go to a server where nothing can be reserved: go back,
    // and say so, rather than report a switch that did not happen (review).
    selectServer(before);
    const back = await switchHeldSession();
    return reply(`${target} answered, but no session could be held there, so this ` +
      `session is back on ${from}` + (back ? "." : ", where its session is not live " +
      "yet either: session_status shows it.") + (released.length
      ? ` ${released.join(", ")} were released on ${from}.` : ""), true);
  }
  return reply({
    current: target, server_url: url, version, ...(released.length ? { released } : {}),
    note: `Every tool call now goes to ${target}. Reserve a device there with reserve_devices.` +
      (releaseNote ? ` ${releaseNote}` : ""),
  });
}

export interface ReserveCriteria {
  device_type?: string;
  os_version?: string;
  name?: string;
  device_family?: string;
  count?: number;
  auto_boot?: boolean;
}

/** The pool's _effective_device_family: the family, or one inferred from the
 * name when a device has none. */
function familyOf(d: Record<string, unknown>): string {
  if (d.device_family) return String(d.device_family);
  const n = String(d.name ?? "").toLowerCase();
  if (n.includes("ipad")) return "iPad";
  if (n.includes("iphone")) return "iPhone";
  if (n.includes("apple watch")) return "Apple Watch";
  if (n.includes("apple tv")) return "Apple TV";
  return "";
}

/** The OS version's number part: "iOS 18.6" -> "18.6". */
function versionOf(v: unknown): string | undefined {
  return String(v ?? "").match(/[\d.]+/)?.[0];
}

/** The free devices on servers other than `current` that reserve_devices'
 * criteria would pick -- the pool's own rules (server/device/pool.py and
 * _choose in server/api/sessions.py): an OS version prefix, exact type and
 * family (inferred from the name when unset), the name or model exact if any
 * is, else a substring; booted, or shut down when auto_boot allows booting;
 * and only servers with `count` of them. Servers that could not be asked are
 * left out. */
export function matchingElsewhere(
  data: unknown, current: string, criteria: ReserveCriteria,
): { name: string; devices: Record<string, unknown>[] }[] {
  const remotes = (data as { remotes?: unknown })?.remotes;
  if (!Array.isArray(remotes)) return [];
  const want = versionOf(criteria.os_version);
  const out: { name: string; devices: Record<string, unknown>[] }[] = [];
  for (const r of remotes as Record<string, unknown>[]) {
    if (!r || r.name === current || !Array.isArray(r.devices)) continue;
    let devices = (r.devices as Record<string, unknown>[]).filter((d) => {
      if (!d.free || d.is_available === false) return false;
      const bootable = criteria.auto_boot !== false && d.state === "shutdown";
      if (d.state !== "booted" && !bootable) return false;
      if (criteria.os_version) {
        const have = versionOf(d.os_version);
        if (!want || !have || !(have === want || have.startsWith(want + "."))) return false;
      }
      if (criteria.device_type && d.device_type !== criteria.device_type) return false;
      if (criteria.device_family &&
          familyOf(d).toLowerCase() !== criteria.device_family.toLowerCase()) {
        return false;
      }
      return true;
    });
    if (criteria.name) {
      const n = criteria.name.toLowerCase();
      const fields = (d: Record<string, unknown>) =>
        [String(d.name ?? "").toLowerCase(), String(d.model ?? "").toLowerCase()].filter(Boolean);
      const exact = devices.filter((d) => fields(d).includes(n));
      devices = exact.length ? exact : devices.filter((d) => fields(d).some((f) => f.includes(n)));
    }
    if (devices.length && devices.length >= (criteria.count ?? 1)) {
      out.push({ name: String(r.name), devices });
    }
  }
  return out;
}

/** A line for reserve_devices' failure: where the devices it could not find
 * here are free, and how to get there. Empty when nowhere else has them or
 * the remotes could not be listed. */
export async function elsewhereHint(criteria: ReserveCriteria, lead = "Not enough here"): Promise<string> {
  let found;
  try {
    found = matchingElsewhere(await quernRemoteList(), currentRemote() ?? LOCAL, criteria);
  } catch {
    return "";
  }
  if (!found.length) return "";
  const parts = found.map(({ name, devices }) => {
    const shown = devices.slice(0, 5).map((d) =>
      `${d.name}${d.state === "booted" ? " (booted)" : ""}`).join(", ");
    const more = devices.length > 5 ? `, and ${devices.length - 5} more` : "";
    return `${name === LOCAL ? "this Mac (local)" : name} has ${devices.length} free that ` +
      `match: ${shown}${more}`;
  });
  const first = found[0].name;
  return `\n\n${lead}, but ${parts.join("; ")}. To use one, switch with ` +
    `use_remote(name="${first}"), then reserve_devices again.`;
}

/** Mark the entry this MCP talks to -- this Mac's server, or the remote
 * QUERN_REMOTE names -- so an agent knows which devices it can reserve now. */
export function markCurrent(data: unknown, current: string): unknown {
  const remotes = (data as { remotes?: unknown })?.remotes;
  if (!Array.isArray(remotes)) return data;
  for (const r of remotes as Record<string, unknown>[]) {
    if (r && typeof r === "object") r.current = r.name === current;
  }
  return data;
}

/** `quern remote list --json`, from the quern CLI on this machine: the paired
 * remotes live in its ~/.quern/remotes.json, whichever server this MCP talks to.
 * It exits 1 when a remote could not be asked, and still prints the list. */
export function quernRemoteList(): Promise<unknown> {
  const candidates = ["quern", `${homedir()}/.local/bin/quern`];
  const attempt = (i: number): Promise<unknown> => new Promise((resolve, reject) => {
    execFile(candidates[i], ["remote", "list", "--json", "--with-local"], { timeout: 60_000 },
      (err, stdout, stderr) => {
        const out = String(stdout ?? "").trim();
        if (out) {
          try {
            resolve(JSON.parse(out));
            return;
          } catch {
            // not JSON: an older quern, or an error; fall through
          }
        }
        const missing = err && (err as NodeJS.ErrnoException).code === "ENOENT";
        const failure = new Error(missing
          ? "the quern CLI is not installed on this machine, so its paired remotes cannot be listed"
          : `quern remote list failed: ${String(stderr || err?.message || "no output").trim()}`);
        if (i + 1 < candidates.length) {
          // Not there, or an older quern on PATH that answers no --json:
          // the installed wrapper may be newer. Its error wins only if it is
          // there too (review).
          attempt(i + 1).then(resolve, (next) => reject(
            (next as Error).message.includes("not installed") && !missing ? failure : next));
          return;
        }
        reject(failure);
      });
  });
  return attempt(0);
}
