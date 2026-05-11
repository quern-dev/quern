import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerLandmarkTools(server: McpServer): void {
  server.registerTool("load_landmarks", {
    description: `Load screen landmarks for an app from a knowledge base directory or inline JSON. Landmarks enable screen identification — matching the current UI state against known screen definitions. Landmarks are scoped by app identifier so multiple apps can be loaded simultaneously.

The response includes a 'skipped' array listing screen files the loader couldn't turn into landmarks, with categorized reasons:
  - legacy_format: file uses the pre-landmarks 'identify_by:' field. Includes the original entries so an agent can propose a migration to the new schema with user review.
  - no_landmarks: file has neither field (likely a stub).
  - no_frontmatter / yaml_error / invalid_entries: file is malformed.

When skipped[] contains legacy_format entries, the recommended workflow is to surface them to the user, propose a per-file migration (see the app-knowledge-guide), and rewrite each file after review.`,
    inputSchema: strictParams({
      app: z
        .string()
        .describe("App identifier (e.g. bundle ID like 'com.example.app')"),
      path: z
        .string()
        .optional()
        .describe(
          "Path to the knowledge base directory containing screens/ with landmark-annotated markdown files"
        ),
      landmarks: z
        .record(
          z.string(),
          z.array(
            z.object({
              element: z.string(),
              identifier: z.string().optional(),
              label: z.string().optional(),
              label_contains: z.string().optional(),
              absent: z.boolean().optional(),
              selected: z
                .boolean()
                .optional()
                .describe(
                  "Selection state for tabs, switches, radios, checkboxes. " +
                  "true = element must be selected (e.g. the active tab); " +
                  "false = element must not be selected. Omit to ignore."
                ),
            })
          )
        )
        .optional()
        .describe(
          "Inline landmarks: object keyed by screen name, each value is an array of landmark selectors"
        ),
    }),
  }, async ({ app, path, landmarks }) => {
    try {
      const body: Record<string, unknown> = { app };
      if (path) body.source = path;
      if (landmarks) body.landmarks = landmarks;
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
    description: `Identify the current screen by matching the live UI tree against loaded landmarks. Returns the matched screen name, confidence level (exact/ambiguous/none), and partial matches. Load landmarks first with load_landmarks.

partial_matches contains EVERY non-fully-matched screen (including zero-match), sorted by descending match count so the best candidate is first. Each entry has a 'landmarks' array with per-landmark match results, so you can debug "why didn't my landmarks match?" without re-running identification — the failing selectors are right there in the response.`,
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
    description: `List all loaded landmark sets, showing the app identifier and number of screens for each.`,
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
    description: `Unload landmarks for a specific app or all apps. Frees the memory used by landmark definitions.`,
    inputSchema: strictParams({
      app: z
        .string()
        .optional()
        .describe("App to unload (omit to unload all)"),
    }),
  }, async ({ app }) => {
    try {
      const params: Record<string, string> = {};
      if (app) params.app = app;
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
    description: `Check for landmark collisions across screens. Reports pairs of screens whose landmarks overlap (one could be mistaken for the other) and screens with no landmarks defined. Can validate loaded landmarks or scan a knowledge base path directly.`,
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
      const body: Record<string, unknown> = {};
      if (app) body.app = app;
      if (path) body.source = path;
      const data = await apiRequest("POST", "/api/v1/landmarks/validate", undefined, body);
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
