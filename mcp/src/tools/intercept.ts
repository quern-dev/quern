import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { apiRequest } from "../http.js";
import { strictParams } from "./helpers.js";

export function registerInterceptTools(server: McpServer): void {
  server.registerTool(
    "set_intercept",
    {
      description: `Set an intercept pattern on the proxy. Matching requests will be held (paused) until you release them.

Filter operators: ~d (domain), ~u (URL/path regex), ~m (method), ~c (status code), ~h (header), ~t (content-type), ~b (body). Combine with & (and), | (or), ! (not). Note: ~p is not a valid operator — use ~u for path matching.`,
      inputSchema: strictParams({
        pattern: z
          .string()
          .describe(
            'Filter pattern (e.g. "~d api.example.com & ~u /v1/users", "~m POST & ~d api.example.com")'
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
          .coerce.number()
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
              .record(z.string(), z.string())
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
          .record(z.string(), z.string())
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
      description: `Add a mock response rule. Requests matching the pattern will receive a synthetic response instead of hitting the real server. Mock rules take priority over intercept.

Filter operators: ~d (domain), ~u (URL/path regex), ~m (method), ~c (status code), ~h (header), ~t (content-type), ~b (body). Combine with & (and), | (or), ! (not). Note: ~p is not a valid operator — use ~u for path matching.

ONE SIMULATOR: pass simulator_udid to mock only that simulator's requests. Every other device's matching requests reach the real server and are captured as usual, so other simulators (CI runs, other agents) are unaffected. No filter pattern can do this: every simulator's traffic arrives from 127.0.0.1. Quern decides from the process that opened the connection, walked up the live process tree, so it covers that simulator's web views too (its WebKit and Safari traffic). A request quern cannot attribute is NOT mocked by a scoped rule. Needs local capture to be reliable; with only the system proxy, the connection's process may not be known yet when the request arrives. Can't mock HTTPS from a simulator whose TLS is passed through because it does not trust the CA, and never mocks replay_flow (replays come from quern itself). The response's warning says when any of these applies, or when the UDID is not a booted simulator.

ORDER: rules scoped to the requesting simulator are checked before unscoped ones, so a catch-all never shadows a simulator's own mock; within each group, the first rule set wins, and update_mock keeps a rule's place.

A mocked request is recorded once, like any other flow, with mock_rule_id set and the tag "mocked", so captured traffic tells synthetic responses from real ones.`,
      inputSchema: strictParams({
        pattern: z
          .string()
          .describe('Filter pattern (e.g. "~d api.example.com & ~u /v1/users", "~m POST & ~u /v1/login")'),
        simulator_udid: z
          .string()
          .optional()
          .describe(
            "Mock only this simulator's requests (its UDID, from list_devices). Omit to mock every device."
          ),
        status_code: z
          .coerce.number()
          .default(200)
          .describe("HTTP status code for the mock response"),
        headers: z
          .record(z.string(), z.string())
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
    async ({ pattern, simulator_udid, status_code, headers, body }) => {
      try {
        const response: Record<string, unknown> = {
          status_code,
          body,
        };
        if (headers) {
          response.headers = headers;
        }
        const payload: Record<string, unknown> = { pattern, response };
        if (simulator_udid !== undefined) payload.simulator_udid = simulator_udid;

        const data = await apiRequest(
          "POST",
          "/api/v1/proxy/mocks",
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
    "list_mocks",
    {
      description: `List all active mock response rules, each with its simulator_udid scope (null means it mocks every device).`,
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
      description: `Update an existing mock rule's pattern, response fields or simulator scope. Only provided fields are changed: omitting simulator_udid keeps the rule's scope, a UDID re-scopes it, and null makes it mock every device.`,
      inputSchema: strictParams({
        rule_id: z.string().describe("The mock rule ID to update"),
        pattern: z
          .string()
          .optional()
          .describe(
            'New mitmproxy filter pattern (e.g. "~d api.example.com")'
          ),
        status_code: z
          .coerce.number()
          .optional()
          .describe("New HTTP status code for the mock response"),
        headers: z
          .record(z.string(), z.string())
          .optional()
          .describe("New response headers (replaces all headers)"),
        body: z
          .string()
          .optional()
          .describe("New response body string"),
        simulator_udid: z
          .string()
          .nullable()
          .optional()
          .describe(
            "A UDID to scope the rule to that simulator, or null to mock every device. Omit to keep the current scope."
          ),
      }),
    },
    async ({ rule_id, pattern, status_code, headers, body, simulator_udid }) => {
      try {
        const payload: Record<string, unknown> = {};
        if (pattern !== undefined) {
          payload.pattern = pattern;
        }
        // undefined keeps the scope; null is sent, and clears it.
        if (simulator_udid !== undefined) {
          payload.simulator_udid = simulator_udid;
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
      description: `Clear mock response rules. If rule_id is provided, removes only that rule, and errors with 404 if no such rule exists — a rule you did not remove is still matching traffic, so teardown can check this. Omitting rule_id removes all mock rules and succeeds even when there were none -- the response's "count" says how many actually went, so that case is still detectable.`,
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
