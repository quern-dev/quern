import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerInterceptTools(server: McpServer): void {
  server.registerTool(
    "set_intercept",
    {
      description: `Set an intercept pattern on the proxy. Matching requests will be held (paused) until you release them. Uses mitmproxy filter syntax (e.g. "~d api.example.com", "~m POST & ~d api.example.com").`,
      inputSchema: strictParams({
        pattern: z
          .string()
          .describe(
            'mitmproxy filter pattern (e.g. "~d api.example.com", "~m POST & ~d api.example.com")'
          ),
      }),
    },
    async ({ pattern }) => {
      try {
        const data = await apiRequest(
          "POST",
          "/api/v1/proxy/intercept",
          undefined,
          { pattern }
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool(
    "clear_intercept",
    {
      description: `Clear the intercept pattern and release all held flows. Flows will complete normally.`,
      inputSchema: strictParams({}),
    },
    async () => {
      try {
        const data = await apiRequest("DELETE", "/api/v1/proxy/intercept");

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
    }
  );

  server.registerTool(
    "list_held_flows",
    {
      description: `List flows currently held by the intercept filter. Supports long-polling: set timeout > 0 to block until a flow is intercepted or timeout expires. This is the recommended approach for MCP agents — make a single blocking call instead of rapid polling.`,
      inputSchema: strictParams({
        timeout: z
          .number()
          .min(0)
          .max(60)
          .default(0)
          .describe(
            "Long-poll timeout in seconds. 0 = return immediately. >0 = block until a flow is caught or timeout expires."
          ),
      }),
    },
    async ({ timeout }) => {
      try {
        const data = await apiRequest("GET", "/api/v1/proxy/intercept/held", {
          timeout,
        });

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
    }
  );

  server.registerTool(
    "release_flow",
    {
      description: `Release a held flow, optionally modifying the request before it continues. Modifications can include changes to headers, body, URL, or method.`,
      inputSchema: strictParams({
        flow_id: z.string().describe("The held flow ID to release"),
        modifications: z
          .object({
            headers: z
              .record(z.string())
              .optional()
              .describe("Headers to add/override"),
            body: z.string().optional().describe("New request body"),
            url: z.string().optional().describe("New request URL"),
            method: z.string().optional().describe("New HTTP method"),
          })
          .optional()
          .describe("Optional request modifications to apply before releasing"),
      }),
    },
    async ({ flow_id, modifications }) => {
      try {
        const data = await apiRequest(
          "POST",
          "/api/v1/proxy/intercept/release",
          undefined,
          { flow_id, modifications: modifications || null }
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool(
    "replay_flow",
    {
      description: `Replay a previously captured HTTP flow through the proxy. The replayed request appears as a new flow in captures. Optionally modify headers or body.`,
      inputSchema: strictParams({
        flow_id: z.string().describe("The captured flow ID to replay"),
        modify_headers: z
          .record(z.string())
          .optional()
          .describe("Headers to add/override on the replayed request"),
        modify_body: z
          .string()
          .optional()
          .describe("New body for the replayed request"),
      }),
    },
    async ({ flow_id, modify_headers, modify_body }) => {
      try {
        const body: Record<string, unknown> = {};
        if (modify_headers) body.modify_headers = modify_headers;
        if (modify_body !== undefined) body.modify_body = modify_body;

        const data = await apiRequest(
          "POST",
          `/api/v1/proxy/replay/${encodeURIComponent(flow_id)}`,
          undefined,
          Object.keys(body).length > 0 ? body : undefined
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool(
    "set_mock",
    {
      description: `Add a mock response rule. Requests matching the pattern will receive a synthetic response instead of hitting the real server. Mock rules take priority over intercept. Uses mitmproxy filter syntax.`,
      inputSchema: strictParams({
        pattern: z
          .string()
          .describe('mitmproxy filter pattern (e.g. "~d api.example.com & ~m POST")'),
        status_code: z
          .number()
          .default(200)
          .describe("HTTP status code for the mock response"),
        headers: z
          .record(z.string())
          .optional()
          .describe(
            'Response headers (default: {"content-type": "application/json"})'
          ),
        body: z
          .string()
          .default("")
          .describe("Response body string"),
      }),
    },
    async ({ pattern, status_code, headers, body }) => {
      try {
        const response: Record<string, unknown> = {
          status_code,
          body,
        };
        if (headers) {
          response.headers = headers;
        }

        const data = await apiRequest(
          "POST",
          "/api/v1/proxy/mocks",
          undefined,
          { pattern, response }
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool(
    "list_mocks",
    {
      description: `List all active mock response rules.`,
      inputSchema: strictParams({}),
    },
    async () => {
      try {
        const data = await apiRequest("GET", "/api/v1/proxy/mocks");

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
    }
  );

  server.registerTool(
    "update_mock",
    {
      description: `Update an existing mock rule's pattern and/or response fields. Only provided fields are changed.`,
      inputSchema: strictParams({
        rule_id: z.string().describe("The mock rule ID to update"),
        pattern: z
          .string()
          .optional()
          .describe(
            'New mitmproxy filter pattern (e.g. "~d api.example.com")'
          ),
        status_code: z
          .number()
          .optional()
          .describe("New HTTP status code for the mock response"),
        headers: z
          .record(z.string())
          .optional()
          .describe("New response headers (replaces all headers)"),
        body: z
          .string()
          .optional()
          .describe("New response body string"),
      }),
    },
    async ({ rule_id, pattern, status_code, headers, body }) => {
      try {
        const payload: Record<string, unknown> = {};
        if (pattern !== undefined) {
          payload.pattern = pattern;
        }
        if (
          status_code !== undefined ||
          headers !== undefined ||
          body !== undefined
        ) {
          const response: Record<string, unknown> = {};
          if (status_code !== undefined) response.status_code = status_code;
          if (headers !== undefined) response.headers = headers;
          if (body !== undefined) response.body = body;
          payload.response = response;
        }

        const data = await apiRequest(
          "PATCH",
          `/api/v1/proxy/mocks/${encodeURIComponent(rule_id)}`,
          undefined,
          payload
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
              text: `Error: ${e instanceof Error ? e.message : String(e)}`,
            },
          ],
          isError: true,
        };
      }
    }
  );

  server.registerTool(
    "clear_mocks",
    {
      description: `Clear mock response rules. If rule_id is provided, removes only that rule. Otherwise removes all mock rules.`,
      inputSchema: strictParams({
        rule_id: z
          .string()
          .optional()
          .describe("Specific mock rule ID to remove. Omit to clear all."),
      }),
    },
    async ({ rule_id }) => {
      try {
        let data;
        if (rule_id) {
          data = await apiRequest(
            "DELETE",
            `/api/v1/proxy/mocks/${encodeURIComponent(rule_id)}`
          );
        } else {
          data = await apiRequest("DELETE", "/api/v1/proxy/mocks");
        }

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
    }
  );
}
