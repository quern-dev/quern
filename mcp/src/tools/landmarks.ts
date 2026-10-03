import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";
import { inlineLandmarks } from "./landmark-schema.js";

export function registerLandmarkTools(server: McpServer): void {
  server.registerTool("load_landmarks", {
    description: `Load screen landmarks for an app from a knowledge base directory or inline JSON. Landmarks enable screen identification — matching the current UI state against known screen definitions. Landmarks are scoped by app identifier so multiple apps can be loaded simultaneously.

Landmarks are held in memory and gone at every restart: set remember=true with a path to load that knowledge base again at every start (list_landmarks shows what is remembered; unload_landmarks with forget=true stops it). With a path inside a project's .quern/, app can be omitted -- the project's .quern/config.json names it.

The response includes a 'skipped' array listing screen files the loader couldn't turn into landmarks, with categorized reasons:
  - legacy_format: file uses the pre-landmarks 'identify_by:' field. Includes the original entries so an agent can propose a migration to the new schema with user review.
  - no_landmarks: file has neither field (likely a stub).
  - no_frontmatter / yaml_error / invalid_entries: file is malformed.

When skipped[] contains legacy_format entries, the recommended workflow is to surface them to the user, propose a per-file migration (see the app-knowledge-guide), and rewrite each file after review.

The response also carries a 'conventions' block. Each screen file may declare 'landmark_conventions: N', the conventions it is written for — a target, not a claim; quern never writes it. Quern checks every file against the current conventions anyway and reports counts for all of them plus, in 'files', each one not 'current': 'undeclared' (read as v1), 'behind', 'failing' (declares current but has findings), or 'newer'. Findings name the landmark and why it matches on one backend only (needs_identifier_or_label, needs_identifier, no_portable_counterpart) or is malformed (legacy_format, invalid_declaration). Fix the findings, then set the declaration — see 'Auditing a knowledge base' in docs/screen-landmarks.md.

Element types are matched across backends: a landmark on the accessibility tree's RadioButton matches the Button WDA reports for the same tab item, when the landmark also has a label or identifier.`,
    inputSchema: strictParams({
      app: z
        .string()
        .optional()
        .describe("App identifier (e.g. bundle ID like 'com.example.app'). Optional with a path inside a project's .quern/ -- its config.json names the app."),
      path: z
        .string()
        .optional()
        .describe(
          "Path to the knowledge base directory containing screens/ with landmark-annotated markdown files, or the project root holding .quern/knowledge"
        ),
      landmarks: inlineLandmarks.optional(),
      remember: z
        .boolean()
        .optional()
        .describe("Load this knowledge base again at every quern start (kept in ~/.quern/config.json). list_landmarks shows what is remembered and how it loaded; unload_landmarks(forget=true) stops it."),
    }),
  }, async ({ app, path, landmarks, remember }) => {
    try {
      const body: Record<string, unknown> = {};
      if (app) body.app = app;
      if (path) body.source = path;
      if (landmarks) body.landmarks = landmarks;
      if (remember !== undefined) body.remember = remember;
      const data = await apiRequest("POST", "/api/v1/landmarks/load", undefined, body);

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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("identify_screen", {
    description: `Identify the current screen by matching the live UI tree against loaded landmarks. Returns the matched screen name, confidence level (exact/ambiguous/none), and partial matches. Load landmarks first with load_landmarks; with none loaded for the app, error is no_landmarks_loaded and hint names what is loaded and what to do.

partial_matches contains EVERY non-fully-matched screen (including zero-match), sorted by descending match count so the best candidate is first. Each entry has a 'landmarks' array with per-landmark match results, so you can debug "why didn't my landmarks match?" without re-running identification — the failing selectors are right there in the response.

A landmark that matched only because its element type is named differently on this backend carries matched_via, e.g. "RadioButton≈Button": the landmark was written on the accessibility tree and the screen was read through WDA, or the reverse. Exact matches carry nothing.`,
    inputSchema: strictParams({
      app: z
        .string()
        .optional()
        .describe("Scope matching to a specific app (omit to match against all loaded landmarks)"),
      udid: z
        .string()
        .optional()
        .describe("Target device UDID (defaults to active device)"),
    }),
  }, async ({ app, udid }) => {
    try {
      const body: Record<string, unknown> = {};
      if (app) body.app = app;
      if (udid) body.udid = udid;
      const data = await apiRequest("POST", "/api/v1/landmarks/identify", undefined, body);

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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("list_landmarks", {
    description: `List all loaded landmark sets, showing the app identifier and number of screens for each. 'sources' says where each set was loaded from: a knowledge base path, or 'inline' -- so two checkouts of one app can be told apart. 'remembered' lists the knowledge bases loaded at every start (load_landmarks remember=true): each one's path, whether that path's set is loaded now (loaded, loaded_from), and at_start -- how many screens loaded at this start, or why it did not.`,
    inputSchema: strictParams({}),
  }, async () => {
    try {
      const data = await apiRequest("GET", "/api/v1/landmarks");
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("unload_landmarks", {
    description: `Unload landmarks for a specific app or all apps. Frees the memory used by landmark definitions. With forget=true it also stops a remembered knowledge base loading at every start -- every one of them, if app is omitted.`,
    inputSchema: strictParams({
      app: z
        .string()
        .optional()
        .describe("App to unload (omit to unload all)"),
      forget: z
        .boolean()
        .optional()
        .describe("Also stop loading it at every start, if it was remembered. With no app, every remembered knowledge base is forgotten."),
    }),
  }, async ({ app, forget }) => {
    try {
      const params: Record<string, string> = {};
      if (app) params.app = app;
      if (forget) params.forget = "true";
      const data = await apiRequest("DELETE", "/api/v1/landmarks", params);
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });

  server.registerTool("validate_landmarks", {
    description: `Check for landmark collisions across screens. Reports pairs of screens whose landmarks overlap (one could be mistaken for the other) and screens with no landmarks defined. Can validate loaded landmarks or scan a knowledge base path directly. Also returns the same 'conventions' block as load_landmarks: which files declare the current landmark conventions, and which landmarks would match on one backend only.`,
    inputSchema: strictParams({
      app: z
        .string()
        .optional()
        .describe("Scope validation to a specific app's loaded landmarks"),
      path: z
        .string()
        .optional()
        .describe("Path to knowledge base directory to validate (without loading into registry)"),
    }),
  }, async ({ app, path }) => {
    try {
      // Query parameters, not a body. The handler declares `source` and `app`
      // as bare scalars, which FastAPI reads from the query string -- so a
      // JSON body was silently discarded and every call validated the whole
      // loaded registry instead of the app or path asked for. It was the only
      // landmarks tool sending a body to a handler that takes neither, and it
      // failed the way this repo's failures usually do: a plausible answer to
      // a different question.
      const params: Record<string, string> = {};
      if (app) params.app = app;
      if (path) params.source = path;
      const data = await apiRequest("POST", "/api/v1/landmarks/validate", params);
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
            text: `Error: ${e instanceof Error ? e.message : String(e)}`,
          },
        ],
        isError: true,
      };
    }
  });
}
