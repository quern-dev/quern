import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { currentRemote } from "../config.js";
import { apiRequest } from "../http.js";
import { forgetAllMocks, forgetMock, recordMock, updateMock } from "../carried.js";
// Mock changes wait for a switch under way: one made on the new server while
// the switch was still moving the old one's was moved as well (CodeRabbit).
import { betweenSwitches } from "./system.js";
import { strictParams, errorText } from "./helpers.js";

// The intercepts this session set (#646), by the server it set them on, so a
// bare clear_intercept removes these from the server in use and leaves other
// agents' alone. Keyed by server: after use_remote, the old server's ids mean
// nothing on the new one, and stay listed for when the session goes back.
const mineByServer = new Map<string, Set<string>>();
function mine(): Set<string> {
  const key = currentRemote() ?? "local";
  let ids = mineByServer.get(key);
  if (!ids) mineByServer.set(key, (ids = new Set()));
  return ids;
}
const notFound = (e: unknown) => e instanceof Error && e.message.startsWith("HTTP 404");

export function registerInterceptTools(server: McpServer): void {
  server.registerTool(
    "set_intercept",
    {
      description: `Add an intercept: matching requests are held (paused) until you release them with release_flow, or for 30 seconds, after which each is released unchanged.

Several intercepts can be set at once. Each answer has its rule_id, and each held flow says which intercept held it.

Pass simulator_udid to hold only that device's requests -- a simulator's UDID, or an Android emulator's or phone's serial -- decided as for a scoped mock: a request whose device cannot be told is not held. Prefer it. Without it, the intercept holds every device's and every agent's matching requests, which is owner-key only: check proxy_status's intercepts first, and do not leave one set. A physical iPhone cannot be named here; scope to it in the pattern by its address instead, with ~src <client_ip> (for example "~d api.example.com & ~src 192.168.1.23").

A client key may intercept only on simulators it holds, and its intercepts end with its hold. Call clear_intercept when you are done: releasing one flow leaves its intercept in place.

Filter operators: ~d (domain), ~u (URL/path regex), ~m (method), ~c (status code), ~h (header), ~t (content-type), ~b (body), ~src (client address). Combine with & (and), | (or), ! (not). Note: ~p is not a valid operator — use ~u for path matching.`,
      inputSchema: strictParams({
        pattern: z
          .string()
          .describe(
            'Filter pattern (e.g. "~d api.example.com & ~u /v1/users", "~m POST & ~d api.example.com")'
          ),
        simulator_udid: z
          .string()
          .optional()
          .describe("Hold only this device's requests: a simulator's UDID, or an Android emulator's or phone's serial. Omit to hold every device's (owner key only)."),
      }),
    },
    async ({ pattern, simulator_udid }) => {
      try {
        const body: Record<string, unknown> = { pattern };
        if (simulator_udid) body.simulator_udid = simulator_udid;
        const data = (await apiRequest(
          "POST",
          "/api/v1/proxy/intercept",
          undefined,
          body
        )) as { rule_id?: unknown };
        if (typeof data?.rule_id === "string") mine().add(data.rule_id);

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
              text: errorText(e),
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
      description: `Remove intercepts and release the flows they held; the flows complete normally.

With no arguments it removes the intercepts this session set (#646), and leaves other sessions' alone. rule_id removes one, and errors with 404 if there is no such intercept. all=true removes every intercept on the server: from the owner key that is everyone's, from a client key its own.`,
      inputSchema: strictParams({
        rule_id: z.string().optional().describe("The intercept to remove, from set_intercept or list_held_flows."),
        all: z
          .union([z.boolean(), z.enum(["true", "false"]).transform((v) => v === "true")])
          .optional()
          .describe("Remove every intercept on the server, not only this session's."),
      }),
    },
    async ({ rule_id, all }) => {
      try {
        let data: unknown;
        if (rule_id) {
          try {
            data = await apiRequest("DELETE", `/api/v1/proxy/intercept/${encodeURIComponent(rule_id)}`);
            mine().delete(rule_id);
          } catch (e) {
            // Gone is forgotten; a refusal (423, 503) is kept, so a later
            // clear_intercept tries it again.
            if (notFound(e)) mine().delete(rule_id);
            throw e;
          }
        } else if (all) {
          data = await apiRequest("DELETE", "/api/v1/proxy/intercept");
          mine().clear();
        } else {
          // This session's own, by id: the server cannot tell one owner-key
          // agent's intercepts from another's (as for mocks, #576).
          const removed: string[] = [];
          const gone: string[] = [];
          const failed: Array<{ rule_id: string; error: string }> = [];
          let released = 0;
          for (const id of [...mine()]) {
            try {
              const r = (await apiRequest("DELETE", `/api/v1/proxy/intercept/${encodeURIComponent(id)}`)) as
                { held_flows_released?: number };
              removed.push(id);
              released += r?.held_flows_released ?? 0;
              mine().delete(id);
            } catch (e) {
              // Already gone -- its hold ended, or someone cleared it -- is
              // forgotten. Anything else is reported with the rest, and kept
              // to be tried again, rather than ending the loop and losing
              // what was already removed (review).
              if (notFound(e)) {
                gone.push(id);
                mine().delete(id);
              } else {
                failed.push({ rule_id: id, error: errorText(e) });
              }
            }
          }
          data = {
            status: failed.length ? "partial" : "accepted", count: removed.length,
            rule_ids: removed, held_flows_released: released,
            ...(gone.length ? { already_gone: gone } : {}),
            ...(failed.length ? { failed } : {}),
            note: removed.length || gone.length || failed.length
              ? `${removed.length} of this session's intercept(s) cleared, ${released} held flow(s) released` +
                (failed.length ? `; ${failed.length} could not be removed (see failed), and stay set.` : ".")
              : "This session set no intercepts on this server. all=true removes every intercept on the server.",
          };
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
              text: errorText(e),
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
      description: `List flows currently held by intercepts, each with the rule_id of the intercept that held it, and the intercepts set. Supports long-polling: set timeout > 0 to block until a flow is intercepted or timeout expires. This is the recommended approach for MCP agents — make a single blocking call instead of rapid polling.`,
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
              text: errorText(e),
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
      description: `Release a held flow, optionally modifying the request before it continues. Modifications can include changes to headers, body, URL, or method. A client key may release only flows its own intercepts held (403 otherwise).`,
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
              text: errorText(e),
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
              text: errorText(e),
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

ONE ANDROID DEVICE: pass its serial as simulator_udid (e.g. emulator-5554). An emulator's requests are told by the emulator process that opened the connection, a phone's by its Wi-Fi address, which quern reads over adb (or from device-proxy-config's record); a phone whose address it cannot find is refused. The device must be routed through this proxy (record_device_proxy_config with apply=true): the warning says when it isn't. A request whose device cannot be told is not mocked.

ORDER: rules scoped to the requesting device -- a simulator or an Android device -- are checked before unscoped ones, so a catch-all never shadows a device's own mock; within each group, the first rule set wins, and update_mock keeps a rule's place.

A mocked request is recorded once, like any other flow, with mock_rule_id set and the tag "mocked", so captured traffic tells synthetic responses from real ones.`,
      inputSchema: strictParams({
        pattern: z
          .string()
          .describe('Filter pattern (e.g. "~d api.example.com & ~u /v1/users", "~m POST & ~u /v1/login")'),
        simulator_udid: z
          .string()
          .trim()
          .optional()
          .describe(
            "Mock only this device's requests: a simulator's UDID, or an Android emulator's or phone's serial (e.g. emulator-5554), from list_devices. Omit to mock every device."
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
    ({ pattern, simulator_udid, status_code, headers, body }) => betweenSwitches(async () => {
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
        // Kept as set, so a switch can move it (carried.ts).
        const ruleId = (data as { rule_id?: unknown } | null)?.rule_id;
        if (typeof ruleId === "string") {
          recordMock(ruleId, { pattern, response, simulator_udid: simulator_udid ?? null,
                               platform: (data as { platform?: unknown } | null)?.platform });
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
              text: errorText(e),
            },
          ],
          isError: true,
        };
      }
    })
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
              text: errorText(e),
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
      description: `Update an existing mock rule's pattern, response fields or device scope. Only provided fields are changed: omitting simulator_udid keeps the rule's scope, a simulator UDID or Android serial re-scopes it (a phone's address is read again then), and null makes it mock every device.`,
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
          .trim()
          .nullable()
          .optional()
          .describe(
            "A simulator UDID or Android serial to scope the rule to that device, or null to mock every device. Omit to keep the current scope."
          ),
      }),
    },
    ({ rule_id, pattern, status_code, headers, body, simulator_udid }) => betweenSwitches(async () => {
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
        updateMock(rule_id, { ...payload,
          platform: (data as { platform?: unknown } | null)?.platform } as Parameters<typeof updateMock>[1]);

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
              text: errorText(e),
            },
          ],
          isError: true,
        };
      }
    })
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
    ({ rule_id }) => betweenSwitches(async () => {
      try {
        let data;
        if (rule_id) {
          try {
            data = await apiRequest(
              "DELETE",
              `/api/v1/proxy/mocks/${encodeURIComponent(rule_id)}`
            );
          } catch (e) {
            // Not there is not to be carried either: kept, it came back at
            // the next switch (review). The 404 still reaches the caller.
            if (e instanceof Error && e.message.startsWith("HTTP 404")) forgetMock(rule_id);
            throw e;
          }
          forgetMock(rule_id);
        } else {
          data = await apiRequest("DELETE", "/api/v1/proxy/mocks");
          forgetAllMocks();
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
              text: errorText(e),
            },
          ],
          isError: true,
        };
      }
    })
  );
}
