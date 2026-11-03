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
    description: `Record an app to find hangs, slow frames and where CPU time goes: an iOS app with Instruments (xctrace) on a simulator or a physical device, or an Android app with perfetto on an emulator or phone.

**On iOS this relaunches the app.** xctrace launches it for the profile and terminates it when the profile stops, so whatever was on screen is lost; relaunch it afterwards. It launches slowly under Instruments: wait for its first screen (wait_for_element) before driving it (tap_element, swipe, ...), then call stop_profile, or let time_limit_s end it.

preset: hangs (default; main-thread hangs, each with what the thread was doing: its CPU samples if busy, the system call it waited in if blocked), cpu (where CPU time goes), hitches (dropped or late frames, with Instruments' explanation of each; physical devices only, xctrace refuses it on simulators), launch (the App Launch template).

**Android:** perfetto records the whole device and the app is NOT relaunched -- drive it as it is. presets: hangs (default; input the app left unanswered, frames, CPU, slowest slices), frames, cpu. The deep summary needs trace_processor, which quern downloads on first use (pinned, checksum-verified, ~15 MB) while the first profile records; the answer's trace_processor field and notes say whether it is installed, downloading, failed or turned off, and without it the summary is what the device reported (gfxinfo frames, InputDispatcher hangs when logcat is captured, CPU from /proc).

One profile per device at a time; the device must be reserved by this session when the server is shared. Kept profiles share a disk budget (config.json profiles.max_mb, default 2048); starting one may remove the oldest finished ones, and the answer says which.`,
    inputSchema: strictParams({
      bundle_id: z.string().min(1).describe("Bundle id (iOS) or package (Android) of the app to record. On iOS xctrace launches it; on Android it is not launched -- it must be running, or be launched during the profile, for the trace to hold its work"),
      udid: z.string().optional().describe("Target device UDID (defaults to the active device)"),
      preset: z.enum(["hangs", "cpu", "hitches", "launch", "frames"]).optional().describe("What to record (default hangs). hitches and launch are iOS only, frames Android only"),
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

summary.hangs lists main-thread hangs: start_s (seconds into the recording), duration_ms, kind (Severe Hang, Hang, Microhang). A hang in the first few seconds can be the app's own cold launch. With Xcode 27.2, xctrace triage reads each one further: tier, main_thread_cpu, and nature -- blocked (the thread waited: look at system_calls and stack, not CPU), busy (it computed: stack is its CPU samples) or mixed; system_calls, the calls it waited in; and for the longest three, stack, the main thread's heaviest stack over the hang from the app's own frames down, with stack_from saying whether it came from system calls or CPU samples. A blocked hang in a profile without System Call Trace (recorded by the hangs preset) has no stack, and stack_note says why; summary.hang_tiers counts hangs by tier, and an older Xcode gives unavailable.triage instead. summary.hitches lists late frames with Instruments' explanation. summary.time_profile has top_functions (where CPU time was spent) and top_app_functions (the app's own code nearest that time). With the launch preset, summary.launch has the launch phases with Instruments' explanation of each, and first_frame_ms, when the first frame was drawn; a physical device records these, a simulator none. A section the template did not record, or that could not be read, is null with its reason in summary.unavailable: no hangs found ([]) and hangs not read (null) are different answers.

**Android summaries:** summary.hangs lists input the app left unanswered for 250 ms or more (start_s, duration_ms; hangs_source says whether from the trace or InputDispatcher's logcat reports), summary.frames the main thread's frame times (count, p50/p90/p99/max, over_budget) or gfxinfo's counts, summary.cpu the app's CPU (by thread, from the trace), summary.slowest_slices its longest work, and summary.trace_processor whether the trace itself was read.`,
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
    description: "A profile's state and, once finished, its summary -- the same as stop_profile returns. state is recording, finalizing (the trace is being finished and read: on iOS xctrace saving it, on Android quern copying it off the device; stopping it again does nothing), finished, or failed (iOS: xctrace did not save a whole trace, or it could not be read; Android: the trace could not be copied off the device, and error names where it is still kept there). Without profile_id, lists the kept profiles, oldest first, without summaries. A profile ended by its time limit finishes on its own; read it here.",
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
    description: "Copy a profile to a directory on this machine: summary.json, its log and, once finished, the trace -- profile.trace.zip for iOS (unzip it and open profile.trace in Instruments), profile.pftrace for Android (open it at ui.perfetto.dev). A failed profile has no trace to download, only its log. For when the server is another machine. A client key downloads only its own profiles.",
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
