import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { LOCAL, currentRemote, discoverServer, selectServer, selectedServer } from "../config.js";
import { switchHeldSession } from "../held-session.js";
import { SESSION_ID, ServerTimeout, ServerUnreachable, apiRequest, serverFetch } from "../http.js";
import { loadRemote } from "../remote.js";
import { cleanUpStranded, moveOut } from "../carried.js";
import { osMatches, parseOs, platformOf, type Platform } from "../platform.js";

/** Run after a switch is live: loads this project's knowledge base on the new
 * server, as at start (index.ts wires it in). Its answer goes into use_remote's. */
let afterSwitch: (() => Promise<unknown>) | null = null;
export function whenSwitched(fn: (() => Promise<unknown>) | null): void {
  afterSwitch = fn;
}
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

One server at a time: devices you hold on the current server must be released first (release_devices), or pass release=true to release them as part of the switch -- also needed when the current server cannot say what you hold. The switch is made only once your session is live on the new server; if it cannot be, you stay where you were.

What you set up follows you: the mocks you set are removed from the old server and set up again on the devices of your next reservation on the new one -- each on the kind of device it was scoped to -- with the session capture you asked for on its simulators, and the Android devices you routed through the proxy (record_device_proxy_config apply=true, held) are unrouted by the old server and the new reservation's Android devices routed through the new proxy (that answer's "carried" lists the new rule ids); this project's knowledge base is loaded there. Mocks the old server could not remove are listed in "carried.still_on_old"; switching back to that server tries again, and "left_here_earlier" then says which went and which are still_there. The answer's "proxy" says whether the new server's proxy runs -- mocks, capture and routed Android devices need it, and only its owner can start it. Machine-wide proxy settings (local capture, system proxy, intercept, bypass), recordings, logging and device settings stay where they were. The quern CLI in your shell is not switched; it still talks to this Mac's server.`,
    inputSchema: strictParams({
      name: z.string().describe(`A server's name from list_remotes, or "local" for this Mac's own`),
      release: z.boolean().optional().describe("Release the devices you hold on the current server as part of the switch (default false: refuse)"),
    }),
  }, (args) => {
    // One switch at a time: two at once each restored the other's "before",
    // leaving requests on one server and the held session on another (review).
    return betweenSwitches(() => useRemote(args));
  });
}

let switching: Promise<unknown> = Promise.resolve();

/** Run `fn` when no switch is under way, and keep switches waiting until it is
 * done: switches, and the reservations a switch must account for. */
export function betweenSwitches<T>(fn: () => Promise<T>): Promise<T> {
  const run = switching.then(fn);
  switching = run.catch(() => undefined);
  return run;
}

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
  // The server being left did not answer at all: nothing more is asked of
  // it -- each request would wait out its own 5 s, three in a row (15 s) --
  // and what could not be removed there is cleaned up if the agent returns.
  let oldUnreachable = false;
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
      oldUnreachable = e instanceof ServerTimeout || e instanceof ServerUnreachable;
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
  if (release && oldUnreachable) {
    releaseNote = `Could not reach ${from} to release anything held there: it is freed when ` +
      "its connection closes or its grace period ends.";
  } else if (release && (held === null || held.length)) {
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
  const old = discoverServer();         // the server being left, for its mocks
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
  // What this agent set up follows it: its mocks leave the old server and,
  // with its session capture, are set up again at its next reservation here.
  const carried = await moveOut(old, from, { unreachable: oldUnreachable });
  // Rules an earlier switch left here, when it could not reach this server.
  const strandedHere = await cleanUpStranded(target);
  let knowledge: unknown;
  try {
    knowledge = afterSwitch ? await afterSwitch() : undefined;
  } catch (e) {
    knowledge = { kind: "failed", reason: errorText(e) };
  }
  const there = await proxyThere();
  const waiting = carried.mocks + carried.session_capture + (carried.device_proxy ? 1 : 0);
  return reply({
    current: target, server_url: url, version, ...(released.length ? { released } : {}),
    ...(waiting || carried.still_on_old ? { carried } : {}),
    ...(strandedHere ? { left_here_earlier: strandedHere } : {}),
    ...(knowledge !== undefined ? { knowledge } : {}),
    proxy: there,
    note: `Every tool call now goes to ${target}. Reserve a device there with reserve_devices.` +
      (carried.mocks + carried.session_capture ? ` Your ${carried.mocks} mock(s) and ` +
        `${carried.session_capture} session capture(s) moved with you: they are set up on the ` +
        "simulators of your next reservation here, and its answer lists their new rule ids."
        : "") +
      (carried.device_proxy ? " The Android devices of your next reservation here are routed " +
        "through this server's proxy, as you routed devices on the last one." : "") +
      (carried.still_on_old ? ` Could not remove ${carried.still_on_old.join(", ")} from ` +
        `${from}: they still answer traffic there. Switching back there tries again to remove ` +
        "them (see left_here_earlier then)." : "") +
      (strandedHere?.removed.length ? ` Removed ${strandedHere.removed.join(", ")}, left ` +
        `here by an earlier switch that could not reach ${target}.` : "") +
      (strandedHere?.still_there.length ? ` Could not remove ${strandedHere.still_there.join(", ")}, ` +
        "left here earlier: they may still answer traffic here, and your mocks they would " +
        "shadow are not set up again on their simulators until they are gone." : "") +
      (releaseNote ? ` ${releaseNote}` : "") +
      " Recordings, logging, previews and device settings stay with the old server and its devices.",
  });
}

/** Whether mocks and capture can work on the server switched to: its proxy
 * running, and what it captures. Says when it could not ask, never "stopped". */
async function proxyThere(): Promise<Record<string, unknown>> {
  try {
    const s = (await apiRequest("GET", "/api/v1/proxy/status", undefined, undefined, 5000)) as
      { status?: string; local_capture?: string[]; mock_rules_count?: number } | null;
    if (typeof s?.status !== "string") {
      return { status: "unknown", note: "its proxy status did not say whether it runs" };
    }
    return {
      status: s.status, local_capture: s.local_capture ?? [],
      ...(s.status === "running" ? {} : { note: `Its proxy is ${s.status}, so mocks and ` +
        "capture cannot work there until it is started (start_proxy, which takes the " +
        "server's owner key)." }),
    };
  } catch (e) {
    return { status: "unknown", note: `could not ask (${errorText(e)})` };
  }
}

export interface ReserveCriteria {
  /** The platform the request was made on (#582): the agent's, which
   * restricts, or the project's, with platform_source, which the server may
   * cross for as many matches on the other platform as the request needs. */
  platform?: Platform;
  platform_source?: string;
  device_type?: string;
  os_version?: string;
  name?: string;
  device_family?: string;
  count?: number;
  auto_boot?: boolean;
  /** list_devices' rules (listed), not reserve_devices' (reservable). */
  listing?: boolean;
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

/** Exact (case-insensitive, underscores as spaces) on any of `fields` if one
 * is, else a substring: "Pixel 7" finds the AVD Pixel_7_API_34 (#582). */
function byName(devices: Record<string, unknown>[], name: string,
                fields: (d: Record<string, unknown>) => string[]): Record<string, unknown>[] {
  const fold = (t: string) => t.replace(/_/g, " ").toLowerCase();
  const n = fold(name);
  const of = (d: Record<string, unknown>) => fields(d).map(fold).filter(Boolean);
  const exact = devices.filter((d) => of(d).includes(n));
  return exact.length ? exact : devices.filter((d) => of(d).some((f) => f.includes(n)));
}

/** What reserve_devices would pick: the pool's rules (server/device/pool.py
 * and _choose in server/api/sessions.py). */
function reservable(devices: Record<string, unknown>[], c: ReserveCriteria): Record<string, unknown>[] {
  const out = devices.filter((d) => {
    if (d.is_available === false) return false;
    const bootable = c.auto_boot !== false && d.state === "shutdown";
    if (d.state !== "booted" && !bootable) return false;
    if (c.os_version && !osMatches(d.os_version, d.device_type, c.os_version)) return false;
    if (c.device_type && d.device_type !== c.device_type) return false;
    if (c.device_family && familyOf(d).toLowerCase() !== c.device_family.toLowerCase()) return false;
    return true;
  });
  return c.name ? byName(out, c.name, (d) => [String(d.name ?? ""), String(d.model ?? "")]) : out;
}

/** What list_devices would list (server/api/device.py): type and platform,
 * the OS version prefix, the stored family (nothing inferred, CodeRabbit),
 * then the name or model, exact before substring. */
function listed(devices: Record<string, unknown>[], c: ReserveCriteria): Record<string, unknown>[] {
  let out = c.device_type ? devices.filter((d) => d.device_type === c.device_type) : devices;
  if (c.os_version) out = out.filter((d) => osMatches(d.os_version, d.device_type, c.os_version));
  if (c.device_family) {
    const want = c.device_family.toLowerCase();
    out = out.filter((d) => String(d.device_family ?? "").toLowerCase() === want);
  }
  if (c.name) out = byName(out, c.name, (d) => [String(d.name ?? ""), String(d.model ?? "")]);
  return out;
}

/** The free devices on servers other than `current` that reserve_devices'
 * criteria would pick -- the pool's own rules (server/device/pool.py and
 * _choose in server/api/sessions.py): an OS version prefix, exact type and
 * family (inferred from the name when unset), the name or model exact if any
 * is, else a substring; booted, or shut down when auto_boot allows booting;
 * and only servers with `count` of them. Servers that could not be asked are
 * left out. */
/** The server's platform rule (server/device/platform.py) over devices that
 * match every other criterion: a platform the request names restricts; the
 * project's is a default, crossed for no more matches than the request
 * needs (a listing: any); with neither, matches on both platforms would be
 * refused as ambiguous, so none are offered (a listing shows both). */
function onePlatform(devices: Record<string, unknown>[], c: ReserveCriteria): Record<string, unknown>[] {
  const named = (c.platform && !c.platform_source ? c.platform : undefined)
    ?? platformOf(c.device_type) ?? parseOs(c.os_version).platform;
  const on = (p: Platform) => devices.filter((d) => platformOf(d.device_type) === p);
  if (named) return on(named);
  const hasCriteria = !!(c.name || c.os_version || c.device_family);
  if (c.platform) {
    const mine = on(c.platform);
    if (mine.length || !hasCriteria) return mine;
    const other = on(c.platform === "ios" ? "android" : "ios");
    return c.listing || other.length <= (c.count ?? 1) ? other : [];
  }
  if (!hasCriteria || c.listing) return devices;
  return on("ios").length && on("android").length ? [] : devices;
}

export function matchingElsewhere(
  data: unknown, current: string, criteria: ReserveCriteria,
): { name: string; devices: Record<string, unknown>[] }[] {
  const remotes = (data as { remotes?: unknown })?.remotes;
  if (!Array.isArray(remotes)) return [];
  const out: { name: string; devices: Record<string, unknown>[] }[] = [];
  for (const r of remotes as Record<string, unknown>[]) {
    if (!r || r.name === current || !Array.isArray(r.devices)) continue;
    const all = r.devices as Record<string, unknown>[];
    // A listing shows held devices too, so its name rule runs over all of
    // them; only the free ones it keeps are offered.
    const devices = onePlatform(criteria.listing ? listed(all, criteria)
      : reservable(all.filter((d) => d.free), criteria), criteria).filter((d) => d.free);
    if (devices.length && devices.length >= (criteria.count ?? 1)) {
      out.push({ name: String(r.name), devices });
    }
  }
  return out;
}

/** A line for reserve_devices' failure: where the devices it could not find
 * here are free, and how to get there. Empty when nowhere else has them or
 * the remotes could not be listed. */
export async function elsewhereHint(
  criteria: ReserveCriteria, lead = "Not enough here", next = "reserve_devices",
): Promise<string> {
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
    `use_remote(name="${first}"), then ${next} again.`;
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
