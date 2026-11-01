import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { createWriteStream } from "node:fs";
import { mkdir, rename, rm, stat } from "node:fs/promises";
import { join, resolve } from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { discoverServer } from "../config.js";
import { apiRequest, quernHeaders, serverFetch } from "../http.js";
import { strictParams, errorText } from "./helpers.js";

function answer(data: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
}

function failure(e: unknown) {
  return {
    content: [{ type: "text" as const, text: errorText(e) }],
    isError: true,
  };
}

const PROFILE_ID = /^prof_[0-9a-f]{12}$/;

function checkId(profile_id: string): string {
  // The server's ids only: the default dest is built from one (see download_recording).
  if (!PROFILE_ID.test(profile_id)) {
    throw new Error(`${JSON.stringify(profile_id)} is not a profile id (prof_ and 12 hex digits, from start_profile)`);
  }
  return encodeURIComponent(profile_id);
}

export function registerProfileTools(server: McpServer): void {
  server.registerTool("start_profile", {
    description: `Record an iOS app with Instruments (xctrace) on a simulator or a physical device, to find hangs, hitches and where CPU time goes.

**This relaunches the app.** xctrace launches it for the profile and terminates it when the profile stops, so whatever was on screen is lost; relaunch it afterwards. It launches slowly under Instruments: wait for its first screen (wait_for_element) before driving it (tap_element, swipe, ...), then call stop_profile, or let time_limit_s end it.

preset: hangs (default; main-thread hangs plus CPU samples), cpu (where CPU time goes), hitches (dropped or late frames, with Instruments' explanation of each; physical devices only, xctrace refuses it on simulators), launch (the App Launch template).

One profile per device at a time; the device must be reserved by this session when the server is shared. Kept profiles share a disk budget (config.json profiles.max_mb, default 2048); starting one may remove the oldest finished ones, and the answer says which. Android is not supported yet: use get_performance.`,
    inputSchema: strictParams({
      bundle_id: z.string().min(1).describe("Bundle id of the app to launch and record"),
      udid: z.string().optional().describe("Target device UDID (defaults to the active device)"),
      preset: z.enum(["hangs", "cpu", "hitches", "launch"]).optional().describe("What to record (default hangs)"),
      time_limit_s: z.number().int().min(1).max(300).optional().describe("Stop by itself after this many seconds (default 30)"),
    }),
  }, async (args) => {
    try {
      // Launching on a device and waiting for xctrace to say it is recording
      // can take most of a minute.
      return answer(await apiRequest("POST", "/api/v1/profiles", undefined, args, 150_000));
    } catch (e) {
      return failure(e);
    }
  });

  server.registerTool("stop_profile", {
    description: `Stop a profile early, wait for xctrace to finish the trace, and return its summary. This can take a minute or more: xctrace finalises the trace (13 s measured for 30 s of recording; 102 s for an iPhone hitches profile), then quern reads its tables. If it is not done within 4 minutes the answer is a 504, and the profile carries on finishing: read it later with get_profile. A 503 means quern could not tell whether xctrace was still running; ask again.

summary.hangs lists main-thread hangs: start_s (seconds into the recording), duration_ms, kind (Severe Hang, Hang, Microhang). A hang in the first few seconds can be the app's own cold launch. summary.hitches lists late frames with Instruments' explanation. summary.time_profile has top_functions (where CPU time was spent) and top_app_functions (the app's own code nearest that time). With the launch preset, summary.launch has the launch phases with Instruments' explanation of each, and first_frame_ms, when the first frame was drawn; a physical device records these, a simulator none. A section the template did not record, or that could not be read, is null with its reason in summary.unavailable: no hangs found ([]) and hangs not read (null) are different answers.`,
    inputSchema: strictParams({
      profile_id: z.string().describe("The profile's id, from start_profile"),
    }),
  }, async ({ profile_id }) => {
    try {
      return answer(await apiRequest("POST", `/api/v1/profiles/${checkId(profile_id)}/stop`, undefined, undefined, 600_000));
    } catch (e) {
      return failure(e);
    }
  });

  server.registerTool("get_profile", {
    description: "A profile's state and, once finished, its summary -- the same as stop_profile returns. state is recording, finalizing (xctrace is saving the trace, then quern reads it; stopping it again does nothing), finished, or failed (xctrace did not save a whole trace, or it could not be read: error says which). Without profile_id, lists the kept profiles, oldest first, without summaries. A profile ended by its time limit finishes on its own; read it here.",
    inputSchema: strictParams({
      profile_id: z.string().optional().describe("A profile's id; omit to list them all"),
    }),
  }, async ({ profile_id }) => {
    try {
      const path = profile_id ? `/api/v1/profiles/${checkId(profile_id)}` : "/api/v1/profiles";
      return answer(await apiRequest("GET", path));
    } catch (e) {
      return failure(e);
    }
  });

  server.registerTool("download_profile", {
    description: "Copy a profile to a directory on this machine: summary.json, xctrace.log and, once finished, the trace as profile.trace.zip (unzip it and open profile.trace in Instruments). A failed profile has no trace to download, only its log. For when the server is another machine. A client key downloads only its own profiles.",
    inputSchema: strictParams({
      profile_id: z.string().describe("The profile's id, from start_profile or get_profile"),
      dest: z.string().optional().describe("Directory to write into, on this machine (default ./quern-profiles/<id>); made if missing"),
    }),
  }, async ({ profile_id, dest }) => {
    try {
      const id = checkId(profile_id);
      const profile = await apiRequest("GET", `/api/v1/profiles/${id}`) as { state: string; files: string[] };
      if (profile.state === "recording" || profile.state === "finalizing") {
        throw new Error(`${profile_id} is still ${profile.state}; stop it or wait, then download`);
      }
      const target = resolve(dest ?? join("quern-profiles", profile_id));
      await mkdir(target, { recursive: true });
      const srv = discoverServer();
      const written: { name: string; bytes: number }[] = [];
      for (const name of profile.files) {
        const resp = await serverFetch(srv, `${srv.url}/api/v1/profiles/${id}/files/${encodeURIComponent(name)}`,
          { headers: quernHeaders(srv.apiKey) }, { noTimeouts: true });
        if (!resp.ok || !resp.body) {
          throw new Error(`HTTP ${resp.status}: ${await resp.text()} (downloading ${name})`);
        }
        const out = join(target, name);
        const part = `${out}.part`;
        try {
          await pipeline(Readable.fromWeb(resp.body as never), createWriteStream(part));
          const expected = Number(resp.headers.get("content-length") ?? NaN);
          const got = (await stat(part)).size;
          if (Number.isFinite(expected) && got !== expected) {
            throw new Error(`the server sent ${got} of ${expected} bytes of ${name}`);
          }
          await rename(part, out);
        } catch (e) {
          await rm(part, { force: true });
          throw e;
        }
        written.push({ name, bytes: (await stat(out)).size });
      }
      return answer({ profile_id, dest: target, files: written });
    } catch (e) {
      return failure(e);
    }
  });
}
