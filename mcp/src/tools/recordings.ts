import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

function answer(data: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] };
}

function failure(e: unknown) {
  return {
    content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
    isError: true,
  };
}

export function registerRecordingTools(server: McpServer): void {
  server.registerTool("start_recording", {
    description: `Record one device's actions, network flows (full detail) and app logs to disk, for as long as a run lasts, until stop_recording. For a long run -- a CI suite of an hour or more -- whose start the in-memory buffers would have evicted by the time anyone looks.

Writes <output_dir>/events.jsonl as things arrive (flushed every second) and <output_dir>/manifest.json. No time limit, and it survives a quern restart: the file gets a gap line for the time quern was down rather than ending. The file says what it does not hold: a "dropped" line where the writer fell behind, a gap line for a restart, and stop_recording's "complete" is true only with neither -- and, with video, only when every movie was finished with a summary to join by.

By default only work positively identified as this device's is recorded; include_unattributed adds flows and lines quern cannot tie to any device, as get_trace does live. Read it back with get_trace(recording=<id or directory>) over any window.`,
    inputSchema: strictParams({
      udid: z.string().describe("The device to record: simulator UDID, Android serial, or physical device UDID"),
      output_dir: z.string().optional().describe("Absolute directory to write into (created if missing; refused if it holds a recording). Default ~/.quern/recordings/<id>"),
      hosts: z.array(z.string()).optional().describe("Only flows to these hosts and their subdomains"),
      exclude_hosts: z.array(z.string()).optional().describe("Drop flows to these hosts and their subdomains, e.g. analytics"),
      kinds: z.array(z.enum(["actions", "flows", "logs"])).optional().describe("What to collect: any of actions (quern's own), flows (full detail), logs (app logs and crash reports). Default all; [\"flows\"] for network calls only"),
      include_unattributed: z.boolean().optional().describe("Also record flows and log lines tied to no device (default false)"),
      video: z.boolean().optional().describe("Simulators only: also record the screen to <output_dir>/video-<n>.mp4 (one movie per quern run), with a keyframe at each action; get_trace(recording=...) then gives each action and flow its {path, offset_s} in the movie. Refused, before anything is written, if the simulator is not booted or is already being filmed by another recording"),
    }),
  }, async (args) => {
    try {
      return answer(await apiRequest("POST", "/api/v1/recordings", undefined, args));
    } catch (e) {
      return failure(e);
    }
  });

  server.registerTool("stop_recording", {
    description: `Stop a recording started with start_recording: writes what is still queued, a final "stopped" line and the manifest. Returns counts by kind, what was dropped, the gaps, and "complete" -- true only when nothing was dropped, quern never stopped during it, and any video asked for was recorded and finished in full (a lost or unjoinable movie is in "warnings").`,
    inputSchema: strictParams({
      recording_id: z.string().describe("The id start_recording returned"),
    }),
  }, async ({ recording_id }) => {
    try {
      return answer(await apiRequest("POST", `/api/v1/recordings/${encodeURIComponent(recording_id)}/stop`));
    } catch (e) {
      return failure(e);
    }
  });

  server.registerTool("get_recording", {
    description: `Read a recording's events -- any combination of actions, flows and logs -- for a window, in the order they were written, a page at a time. For the joined timeline (each action with the flows and logs it caused) use get_trace(recording=...) instead.

detail="summary" (the default here) gives one line per event: a flow's method, URL, status, error and time; an action's name, outcome and duration; a log's level, process and message. Read one flow in full with flow_id. Pages: pass next_cursor back as cursor until it is null. Markers come with every page: "dropped" where the writer fell behind, "paused"/"resumed" around a quern restart; "holes" lists what the recording says it does not hold in this window.`,
    inputSchema: strictParams({
      recording: z.string().describe("The recording's id, or the directory it wrote"),
      kinds: z.array(z.enum(["actions", "flows", "logs"])).optional().describe("Which to read (default all)"),
      since: z.string().optional().describe("Start (ISO 8601)"),
      until: z.string().optional().describe("End (ISO 8601)"),
      detail: z.enum(["summary", "full"]).optional().describe("summary (default) or full records with headers and bodies"),
      flow_id: z.string().optional().describe("Only this flow, in full"),
      cursor: z.number().int().min(0).optional().describe("next_cursor from the previous page"),
      limit: z.number().int().min(1).max(2000).optional().describe("Events per page (default 500)"),
    }),
  }, async ({ recording, kinds, since, until, detail, flow_id, cursor, limit }) => {
    try {
      const params: Record<string, string | number | undefined> = {
        recording, since, until, cursor, limit, flow_id,
        kinds: kinds && kinds.length > 0 ? kinds.join(",") : undefined,
        // Summaries by default: a page of full flows carries every body.
        detail: flow_id ? "full" : detail ?? "summary",
      };
      return answer(await apiRequest("GET", "/api/v1/recordings/events", params));
    } catch (e) {
      return failure(e);
    }
  });

  server.registerTool("list_recordings", {
    description: "The recordings this server is making or has made since it started: device, directory, state, counts, drops and gaps.",
    inputSchema: strictParams({}),
  }, async () => {
    try {
      return answer(await apiRequest("GET", "/api/v1/recordings"));
    } catch (e) {
      return failure(e);
    }
  });
}
